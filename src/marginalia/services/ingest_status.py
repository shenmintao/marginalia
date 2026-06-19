"""Helpers for keeping file ingest state aligned with task outcomes."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import files as files_repo
from marginalia.tasks.kinds import KIND_INGEST_FILE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def mark_file_failed_for_dead_ingest_task(
    session: AsyncSession,
    *,
    task_id: str,
    kind: str,
    payload: Mapping[str, Any] | None,
    reason: str,
) -> bool:
    """Mirror a terminal ingest_file task failure onto files.ingest_status."""
    if kind != KIND_INGEST_FILE or not isinstance(payload, Mapping):
        return False
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return False

    now = _utcnow()
    changed = await files_repo.mark_ingest_failed(
        session, file_id=file_id, now=now,
    )
    if changed:
        await audit_events_repo.append(
            session,
            kind="ingest_status_changed",
            task_id=task_id,
            occurred_at=now,
            payload={
                "file_id": file_id,
                "status": "failed",
                "reason": reason[:500],
            },
        )
    return changed
