"""Reprocess primitive — clear a File's ingest state and re-enqueue it.

Used by:
  - POST /v1/files/{file_id}/reprocess        (user-driven, single)
  - POST /v1/files/reprocess                  (user-driven, bulk)
  - periodic_tick._dispatch_reprocess_low_quality  (self-heal, low-summary)

The mental model: "AI got smarter, redo this." The handler does all the
real work — reprocess just unblocks its write-once gate by clearing
`ingested_at` and purges entry_tags so the new run's tags fully replace
the old. dedup_key matches upload.py:318 so a stale pending/running
ingest_file row short-circuits cleanly.

Caller owns the transaction; this function never commits.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import files as files_repo
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_INGEST_FILE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def reprocess_file(
    session: AsyncSession,
    file_row: File,
    *,
    scheduled_by: str = "reprocess",
) -> str | None:
    """Clear ingest state for one file and enqueue ingest_file.

    Returns the new task_id, or None if dedup short-circuited (a
    pending/running ingest_file row already covers this file).

    `scheduled_by` is recorded in the task_enqueued audit so we can
    distinguish user-driven reprocess from periodic self-heal in logs.
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
        payload={
            "file_id": file_row.id,
            "entry_count": len(entry_ids),
            "scheduled_by": scheduled_by,
        },
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
            "scheduled_by": scheduled_by,
        },
    )
    return task.id
