"""File-level operations — reprocess (single + bulk).

Why these live here and not under /file-entries: reprocess targets the
File row (the content + AI-filled metadata), not a per-position FileEntry.
A single file may have multiple entries across folders; reprocessing
clears `entry_tags` for all of them and re-runs the ingest pipeline once.

The mental model: "user upgraded their LLM, redo the analysis." See
[[feedback-reprocess-scope]] and [[feedback-llm-first-class]].

Implementation: reset state in-line, enqueue KIND_INGEST_FILE with the
same dedup_key as upload.py:318. The existing ingest_file handler does
all the work — we don't introduce a "reprocess" concept, just unblock
its write-once gate by clearing `ingested_at`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File
from marginalia.db.session import get_session
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import files as files_repo
from marginalia.repositories import folders as folders_repo
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_INGEST_FILE

log = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

# Bulk fanout commit chunk size. Each file = ~6 SQL ops (UPDATE File +
# DELETE entry_tags + audit + dedup SELECT + Task INSERT + audit). With
# SQLite a 50-file chunk is well under a second; with Postgres even
# faster. Smaller chunks = more frequent unlocks for concurrent ingest
# workers.
_BULK_CHUNK = 50

# Hard cap on a single bulk request. Keeps any one user from accidentally
# nuking a 100k-file library in one HTTP call. If a real workflow needs
# more, do it in multiple requests.
_BULK_MAX = 5000


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _reset_one(session: AsyncSession, file_row: File) -> str | None:
    """Clear ingest state for one file and enqueue ingest_file.
    Returns task_id or None if dedup short-circuited.

    Caller owns the transaction (no commit here) so bulk paths can
    chunk multiple files into a single commit.
    """
    now = _utcnow()
    entry_ids = await files_repo.list_live_entry_ids_for_file(session, file_row.id)
    for eid in entry_ids:
        await entry_tags_repo.delete_all_for_entry(session, eid)

    file_row.ingested_at = None
    file_row.ingest_status = "pending"
    file_row.updated_at = now

    await audit_events_repo.append(
        session,
        kind="reprocess_requested",
        payload={"file_id": file_row.id, "entry_count": len(entry_ids)},
    )

    task = await enqueue(
        session,
        kind=KIND_INGEST_FILE,
        payload={"file_id": file_row.id},
        dedup_key=f"ingest_file:{file_row.id}",
    )
    if task is None:
        return None
    await audit_events_repo.append(
        session,
        kind="task_enqueued",
        task_id=task.id,
        payload={
            "task_id": task.id,
            "kind": KIND_INGEST_FILE,
            "file_id": file_row.id,
            "scheduled_by": "reprocess",
        },
    )
    return task.id


@router.post("/{file_id}/reprocess", status_code=200)
async def reprocess_one(
    file_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    file_row = await session.get(File, file_id)
    if file_row is None or file_row.deleted_at is not None:
        raise HTTPException(status_code=404, detail="file not found")
    task_id = await _reset_one(session, file_row)
    await session.commit()
    return {
        "file_id": file_id,
        "task_id": task_id,
        "reused": task_id is None,
    }


class BulkReprocessBody(BaseModel):
    file_ids: list[str] | None = None
    catalog_id: str | None = None
    folder_id: str | None = None
    tag_id: str | None = None
    all: bool = False

    @model_validator(mode="after")
    def _exactly_one(self) -> "BulkReprocessBody":
        set_count = sum([
            self.file_ids is not None,
            self.catalog_id is not None,
            self.folder_id is not None,
            self.tag_id is not None,
            self.all,
        ])
        if set_count != 1:
            raise ValueError(
                "exactly one of {file_ids, catalog_id, folder_id, tag_id, all} required"
            )
        if self.file_ids is not None and not self.file_ids:
            raise ValueError("file_ids must be non-empty")
        return self


async def _resolve_file_ids(
    session: AsyncSession, body: BulkReprocessBody,
) -> list[str]:
    if body.file_ids is not None:
        # Filter to live ids — caller may have cached stale ids.
        rows = await files_repo.list_live_ids(session)
        live = set(rows)
        return [fid for fid in body.file_ids if fid in live]
    if body.catalog_id is not None:
        subtree = await catalogs_repo.expand_subtree(session, body.catalog_id)
        return await files_repo.list_live_ids_in_catalogs(session, subtree)
    if body.folder_id is not None:
        # Walk folder subtree, then scope file_entries by folder.
        descendants = await folders_repo.list_live_descendant_ids(
            session, body.folder_id,
        )
        return await files_repo.list_live_ids_in_folders(
            session, [body.folder_id, *descendants],
        )
    if body.tag_id is not None:
        return await files_repo.list_live_ids_with_tag(session, body.tag_id)
    if body.all:
        return await files_repo.list_live_ids(session)
    return []


@router.post("/reprocess", status_code=200)
async def reprocess_bulk(
    body: BulkReprocessBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    file_ids = await _resolve_file_ids(session, body)
    if not file_ids:
        return {"file_count": 0, "task_ids": [], "reused_count": 0, "skipped_count": 0}
    if len(file_ids) > _BULK_MAX:
        raise HTTPException(
            status_code=413,
            detail=f"bulk reprocess limited to {_BULK_MAX} files per request "
                   f"(got {len(file_ids)})",
        )

    task_ids: list[str] = []
    reused_count = 0
    skipped_count = 0

    for i in range(0, len(file_ids), _BULK_CHUNK):
        chunk = file_ids[i : i + _BULK_CHUNK]
        for fid in chunk:
            file_row = await session.get(File, fid)
            if file_row is None or file_row.deleted_at is not None:
                skipped_count += 1
                continue
            tid = await _reset_one(session, file_row)
            if tid is None:
                reused_count += 1
            else:
                task_ids.append(tid)
        await session.commit()

    return {
        "file_count": len(file_ids),
        "task_ids": task_ids,
        "reused_count": reused_count,
        "skipped_count": skipped_count,
    }
