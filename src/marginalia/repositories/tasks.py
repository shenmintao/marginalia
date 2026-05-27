"""tasks repository — pure SA queries against the Task table.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models.tasks import Task


def _release_values(**extras: object) -> dict[str, object]:
    """UPDATE values for any transition that releases the worker lock.
    All five lifecycle terminals (done/dead/pending-retry/pending-revive/
    dead-from-running) share this base; extras specify the new status,
    timestamps, error text, etc."""
    return {"locked_by": None, "lease_expires_at": None, **extras}


async def find_pending_or_running_by_dedup(
    db: AsyncSession, dedup_key: str,
) -> Task | None:
    """Used by enqueue's dedup short-circuit."""
    return (
        await db.execute(
            select(Task).where(
                Task.dedup_key == dedup_key,
                Task.status.in_(("pending", "running")),
            )
        )
    ).scalar_one_or_none()


async def claim_pending_ids(
    db: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> list[str]:
    """Pick the next pending task ids, ordered by `(priority, scheduled_at)`.
    The caller turns these into `running`. Postgres uses FOR UPDATE SKIP
    LOCKED so concurrent workers don't step on each other; SQLite has no
    FOR UPDATE so the caller is expected to be the only worker."""
    stmt = (
        select(Task.id)
        .where(Task.status == "pending", Task.scheduled_at <= now)
        .order_by(Task.priority.asc(), Task.scheduled_at.asc())
        .limit(limit)
    )
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def mark_running(
    db: AsyncSession,
    *,
    ids: Sequence[str],
    now: datetime,
    lease_until: datetime,
    worker_id: str,
) -> None:
    """Bulk-transition the given pending ids to running, bumping attempts."""
    if not ids:
        return
    await db.execute(
        update(Task)
        .where(Task.id.in_(list(ids)), Task.status == "pending")
        .values(
            status="running",
            locked_by=worker_id,
            lease_expires_at=lease_until,
            started_at=now,
            attempts=Task.attempts + 1,
        )
    )


async def mark_done(
    db: AsyncSession, *, task_id: str, now: datetime,
) -> None:
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(_release_values(
            status="done", finished_at=now, last_error=None,
        ))
    )


async def mark_dead(
    db: AsyncSession, *, task_id: str, now: datetime, error: str,
) -> None:
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(_release_values(
            status="dead", finished_at=now, last_error=error,
        ))
    )


async def mark_pending_dead_by_kinds(
    db: AsyncSession, *, kinds: Sequence[str], now: datetime, error: str,
) -> int:
    """Mark every pending task whose kind is in `kinds` as dead in one
    UPDATE. Returns the number of rows affected.

    Used at runner startup to clear the queue of LLM-dependent tasks
    when no api_key is configured, so a freshly-installed instance
    doesn't pile up failures from rows queued by an earlier version
    that didn't have the bootstrap guard."""
    if not kinds:
        return 0
    result = await db.execute(
        update(Task)
        .where(Task.status == "pending", Task.kind.in_(list(kinds)))
        .values(_release_values(
            status="dead", finished_at=now, last_error=error,
        ))
    )
    return int(result.rowcount or 0)


async def reschedule_for_retry(
    db: AsyncSession,
    *,
    task_id: str,
    error: str,
    next_run_at: datetime,
) -> None:
    await db.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(_release_values(
            status="pending", last_error=error, scheduled_at=next_run_at,
        ))
    )


async def heartbeat(
    db: AsyncSession, *, task_id: str, lease_until: datetime,
) -> None:
    await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "running")
        .values(lease_expires_at=lease_until)
    )


async def list_stale_running_ids(
    db: AsyncSession, *, now: datetime, limit: int,
) -> list[str]:
    """Running rows whose lease has expired — the worker that owned them
    likely crashed. Used by recover_stuck_tasks."""
    rows = (
        await db.execute(
            select(Task.id)
            .where(
                Task.status == "running",
                Task.lease_expires_at.isnot(None),
                Task.lease_expires_at < now,
            )
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def list_stale_running(
    db: AsyncSession, *, now: datetime,
) -> list[Task]:
    """Full Task rows for stale-running ids — recover_stuck_tasks needs the
    attempt counts and previous lease/locked_by to write an audit event."""
    rows = (
        await db.execute(
            select(Task).where(
                Task.status == "running",
                Task.lease_expires_at.isnot(None),
                Task.lease_expires_at < now,
            )
        )
    ).scalars().all()
    return list(rows)


async def revive_running_to_pending(
    db: AsyncSession, *, task_id: str, now: datetime,
) -> None:
    """Restore a stale-running row to pending so the worker pool can reclaim
    it. Used by recover_stuck_tasks."""
    await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "running")
        .values(_release_values(status="pending", scheduled_at=now))
    )


async def mark_running_dead(
    db: AsyncSession, *, task_id: str, now: datetime, error: str,
) -> None:
    """Status='running' guarded mark_dead — used by recover_stuck_tasks
    when retries are exhausted."""
    await db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "running")
        .values(_release_values(
            status="dead", finished_at=now, last_error=error,
        ))
    )


async def has_inflight_for_kind(db: AsyncSession, kind: str) -> bool:
    """True if there is at least one pending/running row for `kind`. Used
    by periodic_tick to suppress duplicate dispatch."""
    row = (
        await db.execute(
            select(Task.id).where(
                Task.kind == kind,
                Task.status.in_(("pending", "running")),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def last_done_at_for_kind(
    db: AsyncSession, kind: str,
) -> datetime | None:
    """`finished_at` of the most-recent done row for `kind`, or None.
    Used by periodic_tick to enforce per-kind cadence."""
    return (
        await db.execute(
            select(Task.finished_at)
            .where(Task.kind == kind, Task.status == "done")
            .order_by(Task.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get(db: AsyncSession, task_id: str) -> Task | None:
    return await db.get(Task, task_id)


async def count_by_status(db: AsyncSession) -> dict[str, int]:
    """Counts grouped by status. Used by /admin/tasks summaries."""
    rows = (
        await db.execute(
            select(Task.status, func.count()).group_by(Task.status)
        )
    ).all()
    return {s: c for s, c in rows}


async def count_running_and_pending(db: AsyncSession) -> dict[str, int]:
    """Two-row count of {running, pending}. Used by /tasks/running-count."""
    rows = (
        await db.execute(
            select(Task.status, func.count(Task.id))
            .where(Task.status.in_(("running", "pending")))
            .group_by(Task.status)
        )
    ).all()
    counts = {s: int(c) for s, c in rows}
    return {
        "running": counts.get("running", 0),
        "pending": counts.get("pending", 0),
    }


async def list_by_ids(db: AsyncSession, ids: list[str]) -> list[Task]:
    """Task rows whose id is in `ids`. Used by /tend/{run_id} to join the
    dispatch row's recorded task ids back to live state."""
    if not ids:
        return []
    rows = (
        await db.execute(select(Task).where(Task.id.in_(ids)))
    ).scalars().all()
    return list(rows)


async def list_by_status(
    db: AsyncSession,
    *,
    status: str,
    limit: int,
    offset: int = 0,
) -> list[Task]:
    """Paginated rows for one status, newest scheduled first. Used by the
    /admin/tasks listing."""
    rows = (
        await db.execute(
            select(Task)
            .where(Task.status == status)
            .order_by(Task.scheduled_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows)


async def list_recent_with_usage(
    db: AsyncSession, *, limit: int,
) -> list[dict]:
    """Recently-finished tasks joined with their task_outcomes row so the
    StatusBar popover can show duration + tokens + cache % per task.

    Returns plain dicts, not ORM rows, because the join straddles two
    models and the caller (HTTP route) just serialises them."""
    from marginalia.db.models.task_outcomes import TaskOutcome

    rows = (
        await db.execute(
            select(Task, TaskOutcome.detail)
            .join(
                TaskOutcome,
                (TaskOutcome.object_kind == "task")
                & (TaskOutcome.object_id == Task.id),
                isouter=True,
            )
            .where(Task.status.in_(("done", "dead")))
            .order_by(Task.finished_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": t.id,
            "kind": t.kind,
            "status": t.status,
            "started_at": t.started_at,
            "finished_at": t.finished_at,
            "last_error": t.last_error,
            "payload": t.payload or {},
            "detail": detail or {},
        }
        for t, detail in rows
    ]
