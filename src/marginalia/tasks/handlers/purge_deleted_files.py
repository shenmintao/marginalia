"""purge_deleted_files — DESIGN.md §9.4 + §14.2.2.

Honor user soft-delete intent. Walks file_entries that are past their
`purge_after` timestamp and physically deletes them. If a file has no live
entries left after the purge, the file row + its storage object are also
removed.

Flow per entry:
  1. SELECT file_entries WHERE deleted_at IS NOT NULL AND purge_after < now
  2. For each:
       a. DELETE the file_entry row (FK CASCADE drops entry_tags)
       b. If no other live entries reference the same file_id:
            - DELETE the files row
            - mark its storage object for deletion (best-effort, after commit)
            - audit `file_purged`
       c. audit `entry_purged`

Storage deletion happens AFTER the DB commit succeeds. If storage delete
fails, we log + audit `storage_delete_failed` but do NOT roll back the DB
delete — the file is gone from the user's view either way; we'll garbage-
collect orphaned blobs later.

AI write boundary: AI never enqueues purge for a row it didn't see the user
soft-delete first. This handler ONLY consumes user intent; it never decides
to delete on its own.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import files as files_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.storage import get_storage
from marginalia.storage.base import StorageBackend
from marginalia.tasks.kinds import KIND_PURGE_DELETED_FILES, task_handler

log = logging.getLogger(__name__)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@task_handler(KIND_PURGE_DELETED_FILES)
async def handle_purge_deleted_files(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    storage = get_storage()

    pending_storage_deletes: list[tuple[str, str]] = []  # (file_id, storage_key)
    entries_purged = 0
    files_purged = 0

    async with session_scope() as session:
        due_entries = await entries_repo.list_purge_due(session, now)

        for entry in due_entries:
            entry_id = entry.id
            file_id = entry.file_id
            await entries_repo.hard_delete_by_id(session, entry_id)
            await audit_events_repo.append(
                session,
                kind="entry_purged",
                payload={
                    "entry_id": entry_id,
                    "file_id": file_id,
                    "folder_id": entry.folder_id,
                    "display_name": entry.display_name,
                    "deleted_at": entry.deleted_at.isoformat() if entry.deleted_at else None,
                    "purge_after": entry.purge_after.isoformat() if entry.purge_after else None,
                },
            )
            entries_purged += 1

            still_live = await entries_repo.has_live_entry_for_file(session, file_id)
            still_any = await entries_repo.has_any_entry_for_file(session, file_id)

            if not still_live and not still_any:
                from marginalia.db.models import File
                file_row = await session.get(File, file_id)
                if file_row is None:
                    continue
                storage_key = file_row.storage_key
                await files_repo.hard_delete_by_id(session, file_id)
                await audit_events_repo.append(
                    session,
                    kind="file_purged",
                    payload={
                        "file_id": file_id,
                        "sha256": file_row.sha256,
                        "storage_key": storage_key,
                        "size_bytes": file_row.size_bytes,
                    },
                )
                pending_storage_deletes.append((file_id, storage_key))
                files_purged += 1

        await record_outcome(
            session,
            task_kind=KIND_PURGE_DELETED_FILES,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if (entries_purged or files_purged) else "noop",
            detail={
                "entries_purged": entries_purged,
                "files_purged": files_purged,
                "now": now.isoformat(),
            },
        )
        await session.commit()

    # Storage delete is best-effort and runs AFTER commit so a transient S3
    # outage cannot block DB cleanup.
    for file_id, key in pending_storage_deletes:
        await _delete_storage_object(storage, file_id=file_id, key=key)

    if entries_purged or files_purged:
        log.info(
            "purge_deleted_files: entries=%d files=%d", entries_purged, files_purged
        )

async def _delete_storage_object(
    storage: StorageBackend, *, file_id: str, key: str
) -> None:
    try:
        await storage.delete(key)
    except Exception as exc:
        log.exception("storage delete failed for file %s key %s", file_id, key)
        try:
            async with session_scope() as session:
                await audit_events_repo.append(
                    session,
                    kind="storage_delete_failed",
                    payload={"file_id": file_id, "storage_key": key, "error": repr(exc)},
                )
                await session.commit()
        except Exception:
            log.exception("could not even audit storage delete failure")
