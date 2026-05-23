"""ingest_file handler — design.md §9.4 + §11.

Flow per task:
  1. SELECT files row + the entry that triggered the upload (any one is fine —
     this handler can run on a file that has multiple entries; we update the
     first non-deleted one if the entry mentioned in payload is gone).
  2. Mark `files.ingest_status = 'processing'` and audit.
  3. Build PipelineContext from folder path + sibling display_names + a tiny
     catalog/tag sketch. Run the pipeline.
  4. Single transaction:
       - If `ingested_at IS NULL`: write files.summary/description/extra/kind
         and set ingested_at (write-once lock).
       - Resolve catalog path (create chain if needed) → entry.catalog_id.
       - Set entry.extra.
       - Resolve tag suggestions (existing → reuse; new → INSERT) and add
         entry_tags rows with source='ingest'.
       - Set files.ingest_status='done'.
       - Audit `ingest_status_changed` with summary of writes.
  5. On exception: mark files.ingest_status='failed', audit, re-raise so the
     task system records the failure (see runner._fail).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, update

from marginalia.db.models import (
    Catalog,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Tag,
)
from marginalia.db.session import session_scope
from marginalia.pipelines import resolve_pipeline
from marginalia.pipelines.base import PipelineContext, PipelineResult, TagSuggestion
from marginalia.services.audit import write_event
from marginalia.storage import get_storage
from marginalia.tasks.kinds import KIND_INGEST_FILE, task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

CATALOG_SKETCH_LIMIT = 30
TAG_VOCAB_LIMIT = 100


@task_handler(KIND_INGEST_FILE)
async def handle_ingest_file(payload: Mapping[str, Any]) -> None:
    file_id = payload.get("file_id")
    if not file_id:
        raise ValueError("ingest_file payload missing file_id")

    storage = get_storage()

    # --- phase 1: mark processing ------------------------------------------
    async with session_scope() as session:
        file_row = await session.get(File, file_id)
        if file_row is None:
            raise ValueError(f"file_id {file_id!r} not found")
        if file_row.deleted_at is not None:
            log.info("file %s already deleted; skipping ingest", file_id)
            await session.commit()
            return
        if file_row.ingested_at is not None:
            log.info("file %s already ingested; skipping", file_id)
            await session.commit()
            return

        await session.execute(
            update(File)
            .where(File.id == file_id)
            .values(ingest_status="processing", updated_at=_utcnow())
        )
        await write_event(
            session,
            kind="ingest_status_changed",
            payload={"file_id": file_id, "status": "processing"},
        )

        snapshot_storage_key = file_row.storage_key
        snapshot_sha = file_row.sha256
        snapshot_size = file_row.size_bytes
        snapshot_mime = file_row.mime_type
        snapshot_ext = file_row.original_ext

        # Choose the entry we'll attach AI fields to: the oldest non-deleted
        # one. (Multiple entries with same file_id can exist via dedup; the
        # other entries get filled later if they're new — see services.upload
        # which already seeds them on dedup.)
        entry = (
            await session.execute(
                select(FileEntry)
                .where(FileEntry.file_id == file_id, FileEntry.deleted_at.is_(None))
                .order_by(FileEntry.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if entry is None:
            log.warning("file %s has no live entry; aborting ingest", file_id)
            await write_event(
                session,
                kind="ingest_status_changed",
                payload={"file_id": file_id, "status": "failed", "reason": "no_live_entry"},
            )
            await session.execute(
                update(File).where(File.id == file_id).values(ingest_status="failed")
            )
            await session.commit()
            return

        ctx = await _build_context(
            session,
            entry=entry,
            file_id=file_id,
            storage_key=snapshot_storage_key,
            sha256=snapshot_sha,
            size=snapshot_size,
            mime=snapshot_mime,
            ext=snapshot_ext,
        )
        entry_id = entry.id
        await session.commit()

    pipeline = resolve_pipeline(snapshot_mime, snapshot_ext)
    if pipeline is None:
        await _mark_failed(file_id, reason="no_pipeline_for_mime_or_ext")
        raise ValueError(f"no pipeline for mime={snapshot_mime!r} ext={snapshot_ext!r}")

    # --- phase 2: pipeline -------------------------------------------------
    try:
        result = await pipeline.run(ctx=ctx, storage=storage)
    except Exception:
        await _mark_failed(file_id, reason="pipeline_exception")
        raise

    # --- phase 3: persist --------------------------------------------------
    async with session_scope() as session:
        await _persist(session, file_id=file_id, entry_id=entry_id, result=result)
        await session.commit()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _mark_failed(file_id: str, *, reason: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(File).where(File.id == file_id).values(ingest_status="failed", updated_at=_utcnow())
        )
        await write_event(
            session,
            kind="ingest_status_changed",
            payload={"file_id": file_id, "status": "failed", "reason": reason},
        )
        await session.commit()


async def _build_context(
    session,
    *,
    entry: FileEntry,
    file_id: str,
    storage_key: str,
    sha256: str,
    size: int,
    mime: str | None,
    ext: str | None,
) -> PipelineContext:
    folder_path = await _resolve_folder_path(session, entry.folder_id)
    siblings = (
        await session.execute(
            select(FileEntry.display_name)
            .where(
                FileEntry.folder_id == entry.folder_id,
                FileEntry.id != entry.id,
                FileEntry.deleted_at.is_(None),
            )
            .order_by(FileEntry.display_name)
        )
    ).scalars().all()

    # tiny sketches — pipeline can't see the whole catalog/vocab
    cat_rows = (
        await session.execute(
            select(Catalog.id, Catalog.name, Catalog.parent_id)
            .where(Catalog.deleted_at.is_(None))
            .limit(CATALOG_SKETCH_LIMIT)
        )
    ).all()
    catalog_sketch = [
        {"id": r[0], "name": r[1], "parent_id": r[2]} for r in cat_rows
    ]
    tag_rows = (
        await session.execute(
            select(Tag.name, Tag.facet, Tag.doc_count)
            .where(Tag.alias_of.is_(None))
            .order_by(Tag.doc_count.desc())
            .limit(TAG_VOCAB_LIMIT)
        )
    ).all()
    tag_vocabulary = [
        {"name": r[0], "facet": r[1], "doc_count": r[2]} for r in tag_rows
    ]

    return PipelineContext(
        file_id=file_id,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size,
        mime_type=mime,
        original_ext=ext,
        folder_path=folder_path,
        sibling_names=list(siblings),
        catalog_sketch=catalog_sketch,
        tag_vocabulary=tag_vocabulary,
    )


async def _resolve_folder_path(session, folder_id: str | None) -> str:
    if not folder_id:
        return "/"
    parts: list[str] = []
    cur = await session.get(Folder, folder_id)
    while cur is not None:
        parts.append(cur.name)
        if cur.parent_id is None:
            break
        cur = await session.get(Folder, cur.parent_id)
    return "/" + "/".join(reversed(parts))


async def _persist(
    session,
    *,
    file_id: str,
    entry_id: str,
    result: PipelineResult,
) -> None:
    now = _utcnow()
    file_row = await session.get(File, file_id)
    entry = await session.get(FileEntry, entry_id)
    if file_row is None or entry is None:
        raise ValueError("file/entry vanished mid-ingest")

    # --- write-once enforcement on file content fields --------------------
    if file_row.ingested_at is None:
        file_row.summary = result.summary
        file_row.description = result.description
        file_row.kind = result.kind
        file_row.extra = result.extra
        file_row.ingested_at = now
    file_row.ingest_status = "done"
    file_row.updated_at = now

    # --- entry per-position fields ----------------------------------------
    catalog_id = None
    if result.entry_catalog_path:
        catalog_id = await _resolve_or_create_catalog_path(session, result.entry_catalog_path)
    entry.catalog_id = catalog_id
    entry.extra = result.entry_extra
    entry.updated_at = now

    # --- entry tags --------------------------------------------------------
    for sugg in result.entry_tags:
        tag_id = await _resolve_or_create_tag(session, sugg, now)
        existing = (
            await session.execute(
                select(EntryTag).where(
                    EntryTag.entry_id == entry_id, EntryTag.tag_id == tag_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(EntryTag(
                entry_id=entry_id,
                tag_id=tag_id,
                source="ingest",
                created_at=now,
            ))

    await write_event(
        session,
        kind="ingest_status_changed",
        payload={
            "file_id": file_id,
            "status": "done",
            "kind": result.kind,
            "section_count": len(result.description.get("sections", [])) if isinstance(result.description, dict) else 0,
            "tag_count": len(result.entry_tags),
            "catalog_path": result.entry_catalog_path,
        },
    )


async def _resolve_or_create_catalog_path(session, path: list[str]) -> str | None:
    if not path:
        return None
    parent_id: str | None = None
    now = _utcnow()
    for name in path:
        existing = (
            await session.execute(
                select(Catalog).where(
                    Catalog.parent_id.is_(None) if parent_id is None else Catalog.parent_id == parent_id,
                    Catalog.name == name,
                    Catalog.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = Catalog(
                id=new_id(),
                parent_id=parent_id,
                name=name,
                summary=None,
                description=None,
                extra=None,
                tags=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            await session.flush()
            await write_event(
                session,
                kind="catalog_created",
                payload={"catalog_id": existing.id, "name": name, "parent_id": parent_id},
            )
        parent_id = existing.id
    return parent_id


async def _resolve_or_create_tag(session, sugg: TagSuggestion, now: datetime) -> str:
    existing = (
        await session.execute(
            select(Tag).where(Tag.name == sugg.name, Tag.facet == sugg.facet)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.alias_of:
            return existing.alias_of
        existing.doc_count = (existing.doc_count or 0) + 1
        existing.last_used_at = now
        return existing.id

    tag = Tag(
        id=new_id(),
        name=sugg.name,
        facet=sugg.facet,
        alias_of=None,
        doc_count=1,
        last_used_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(tag)
    await session.flush()
    await write_event(
        session,
        kind="tag_created",
        payload={"tag_id": tag.id, "name": sugg.name, "facet": sugg.facet, "source": "ingest"},
    )
    return tag.id
