"""ingest_file handler — DESIGN.md §9.4 + §11.

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

from marginalia.db.models import (
    Catalog,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Tag,
)
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.pipelines import resolve_pipeline
from marginalia.pipelines.base import PipelineContext, PipelineResult, TagSuggestion
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import tags as tags_repo
from marginalia.semantic.index import refresh_semantic_index_for_file
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

        now = _utcnow()
        file_row.ingest_status = "processing"
        file_row.updated_at = now
        await audit_events_repo.append(
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
        entry = await entries_repo.find_first_live_for_file(session, file_id)
        if entry is None:
            log.warning("file %s has no live entry; aborting ingest", file_id)
            await audit_events_repo.append(
                session,
                kind="ingest_status_changed",
                payload={"file_id": file_id, "status": "failed", "reason": "no_live_entry"},
            )
            file_row.ingest_status = "failed"
            file_row.updated_at = _utcnow()
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
            display_name=entry.display_name,
        )
        entry_id = entry.id
        snapshot_filename = entry.display_name
        await session.commit()

    pipeline = resolve_pipeline(
        snapshot_mime, snapshot_ext, filename=snapshot_filename,
    )
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
    await _refresh_semantic_index(file_id)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _mark_failed(file_id: str, *, reason: str) -> None:
    async with session_scope() as session:
        file_row = await session.get(File, file_id)
        if file_row is not None:
            file_row.ingest_status = "failed"
            file_row.updated_at = _utcnow()
        await audit_events_repo.append(
            session,
            kind="ingest_status_changed",
            payload={"file_id": file_id, "status": "failed", "reason": reason},
        )
        await session.commit()


async def _refresh_semantic_index(file_id: str) -> None:
    try:
        async with session_scope() as session:
            result = await refresh_semantic_index_for_file(session, file_id)
            if result.skipped_reason is None:
                await audit_events_repo.append(
                    session,
                    kind="semantic_index_refreshed",
                    payload={
                        "file_id": file_id,
                        "index_name": result.index_name,
                        "entries_removed": result.entries_removed,
                        "entries_refreshed": result.entries_refreshed,
                        "entries_total": result.entries_total,
                        "total_tokens": result.total_tokens,
                    },
                )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        log.exception("semantic index refresh failed for file %s", file_id)
        async with session_scope() as session:
            await audit_events_repo.append(
                session,
                kind="semantic_index_refresh_failed",
                payload={
                    "file_id": file_id,
                    "error": str(exc)[:500],
                },
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
    display_name: str | None = None,
) -> PipelineContext:
    folder_path = await _resolve_folder_path(session, entry.folder_id)
    siblings = await entries_repo.list_sibling_display_names(
        session, folder_id=entry.folder_id, exclude_entry_id=entry.id,
    )

    # tiny sketches — pipeline can't see the whole catalog/vocab
    cat_rows = await catalogs_repo.list_live_sketch(session, limit=CATALOG_SKETCH_LIMIT)
    catalog_sketch = [
        {"id": cid, "name": name, "parent_id": pid} for cid, name, pid in cat_rows
    ]
    tag_rows = await tags_repo.list_canonical_summaries(session, limit=TAG_VOCAB_LIMIT)
    tag_vocabulary = [
        {"name": n, "facet": f, "doc_count": dc} for n, f, dc in tag_rows
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
        display_name=display_name,
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
    attached_tag_ids = set(await entry_tags_repo.list_tag_ids_for_entry(session, entry_id))
    tags_added = 0
    for sugg in result.entry_tags:
        tag_id = await _resolve_or_create_tag(session, sugg, now)
        if tag_id in attached_tag_ids:
            continue
        session.add(EntryTag(
            entry_id=entry_id,
            tag_id=tag_id,
            source="ingest",
            created_at=now,
        ))
        attached_tag_ids.add(tag_id)
        tags_added += 1
        await _mark_tag_attached(session, tag_id=tag_id, now=now)

    await audit_events_repo.append(
        session,
        kind="ingest_status_changed",
        payload={
            "file_id": file_id,
            "status": "done",
            "kind": result.kind,
            "section_count": len(result.description.get("sections", [])) if isinstance(result.description, dict) else 0,
            "tag_count": len(result.entry_tags),
            "tags_added": tags_added,
            "tags_total": len(attached_tag_ids),
            "catalog_path": result.entry_catalog_path,
        },
    )


async def _resolve_or_create_catalog_path(session, path: list[str]) -> str | None:
    if not path:
        return None
    parent_id: str | None = None
    now = _utcnow()
    for name in path:
        existing = await catalogs_repo.find_live_child_by_name(
            session, parent_id=parent_id, name=name,
        )
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
            await audit_events_repo.append(
                session,
                kind="catalog_created",
                payload={"catalog_id": existing.id, "name": name, "parent_id": parent_id},
            )
        parent_id = existing.id
    return parent_id


async def _resolve_or_create_tag(session, sugg: TagSuggestion, now: datetime) -> str:
    existing = await tags_repo.find_canonical_by_name_facet(
        session, name=sugg.name, facet=sugg.facet,
    )
    if existing is not None:
        if existing.alias_of:
            return existing.alias_of
        return existing.id

    tag = Tag(
        id=new_id(),
        name=sugg.name,
        facet=sugg.facet,
        alias_of=None,
        doc_count=0,
        last_used_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(tag)
    await session.flush()
    await audit_events_repo.append(
        session,
        kind="tag_created",
        payload={"tag_id": tag.id, "name": sugg.name, "facet": sugg.facet, "source": "ingest"},
    )
    return tag.id


async def _mark_tag_attached(session, *, tag_id: str, now: datetime) -> None:
    tag = await session.get(Tag, tag_id)
    if tag is None:
        raise ValueError(f"tag_id {tag_id!r} vanished mid-ingest")
    tag.doc_count = (tag.doc_count or 0) + 1
    tag.last_used_at = now
    tag.updated_at = now
