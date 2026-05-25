"""Task introspection HTTP routes.

These endpoints expose minimal task-queue state for CLI bookkeeping —
e.g. the embedded REPL checks `running-count` before exit so the user
can choose to wait for in-flight ingest work to finish before the
TaskRunner dies with the process. `/tasks/active` returns a small
listing (kind + payload preview + age) for the `/background` command,
so users can see what the worker is actually doing instead of just a
count.

These are not the worker's RPC surface; the worker reads the queue
directly from the DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.session import get_session
from marginalia.repositories import tasks as tasks_repo

router = APIRouter(tags=["tasks"])


@router.get("/tasks/running-count")
async def running_count(
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Count tasks currently in `running` or `pending` status.

    Returned counts include both states because in embedded mode,
    pending tasks won't progress once the CLI exits either — the user
    cares about everything still on the queue, not just the in-flight
    rows.
    """
    return await tasks_repo.count_running_and_pending(db)


_PAYLOAD_KEYS_FOR_LABEL = ("entry_id", "file_id", "session_id", "conversation_id", "path")


def _payload_label(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in _PAYLOAD_KEYS_FOR_LABEL:
        v = payload.get(key)
        if v:
            s = str(v)
            return f"{key}={s[:24] + ('...' if len(s) > 24 else '')}"
    # fall back to first key=value pair for visibility
    for k, v in payload.items():
        s = str(v)
        return f"{k}={s[:24] + ('...' if len(s) > 24 else '')}"
    return ""


@router.get("/tasks/active")
async def list_active(
    db: AsyncSession = Depends(get_session),
    limit: int = 30,
) -> dict[str, list[dict]]:
    """Compact listing of running + pending tasks for the `/background` CLI."""
    running = await tasks_repo.list_by_status(db, status="running", limit=limit)
    pending = await tasks_repo.list_by_status(db, status="pending", limit=limit)
    now = datetime.now(timezone.utc)

    def _row(t) -> dict:
        ref = t.started_at or t.scheduled_at
        if ref is not None and ref.tzinfo is None:
            # SQLite strips tzinfo on round-trip; the column stores UTC.
            ref = ref.replace(tzinfo=timezone.utc)
        age_s = int((now - ref).total_seconds()) if ref else 0
        payload = t.payload or {}
        return {
            "id": t.id,
            "kind": t.kind,
            "label": _payload_label(t.payload),
            "file_id": payload.get("file_id"),
            "entry_id": payload.get("entry_id"),
            "attempts": t.attempts,
            "age_s": max(age_s, 0),
        }

    return {
        "running": [_row(t) for t in running],
        "pending": [_row(t) for t in pending],
    }
