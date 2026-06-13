"""task_outcomes repository — DESIGN.md §8.4.

INSERT-only fact table for "what did task X do to object Y when?".
Read by infrastructure (idempotence / recency lookups), pruned on a
30-day rolling window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import TaskOutcome
from marginalia.utils.ids import new_id


VALID_OUTCOMES = ("applied", "noop", "rejected", "deferred", "error")
GLOBAL_OBJECT_KIND = "global"
GLOBAL_OBJECT_ID = "global"


async def record_outcome(
    session: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
    object_id: str,
    outcome: str,
    detail: Mapping[str, Any] | None = None,
    task_run_id: str | None = None,
    completed_at: datetime | None = None,
) -> TaskOutcome:
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"outcome={outcome!r} not in {VALID_OUTCOMES} (task_kind={task_kind!r})"
        )
    row = TaskOutcome(
        id=new_id(),
        task_kind=task_kind,
        object_kind=object_kind,
        object_id=object_id,
        task_run_id=task_run_id,
        outcome=outcome,
        detail=dict(detail) if detail is not None else None,
        completed_at=completed_at or datetime.now(timezone.utc),
    )
    session.add(row)
    return row


async def has_outcome(
    session: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
    object_id: str,
    since: datetime | None = None,
) -> bool:
    """True if any task_outcomes row exists matching the predicate.

    `since` is optional — when None, "ever". Use this for hard idempotence
    (any record means already done).
    """
    stmt = select(TaskOutcome.id).where(
        TaskOutcome.task_kind == task_kind,
        TaskOutcome.object_kind == object_kind,
        TaskOutcome.object_id == object_id,
    )
    if since is not None:
        stmt = stmt.where(TaskOutcome.completed_at >= since)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def select_object_ids(
    session: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
) -> set[str]:
    """Every object_id this task ever recorded for this object_kind, ignoring
    age. Used by maintenance tasks that need durable idempotence."""
    rows = (
        await session.execute(
            select(TaskOutcome.object_id.distinct()).where(
                TaskOutcome.task_kind == task_kind,
                TaskOutcome.object_kind == object_kind,
            )
        )
    ).scalars().all()
    return set(rows)


async def select_recent_object_ids(
    session: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
    since: datetime,
) -> set[str]:
    """object_ids that this task processed since `since`. Use as a filter
    set when picking candidates."""
    rows = (
        await session.execute(
            select(TaskOutcome.object_id.distinct()).where(
                TaskOutcome.task_kind == task_kind,
                TaskOutcome.object_kind == object_kind,
                TaskOutcome.completed_at >= since,
            )
        )
    ).scalars().all()
    return set(rows)


async def oldest_completed_at(db: AsyncSession) -> datetime | None:
    """Used by prune for the "oldest_before" stat."""
    return (
        await db.execute(select(func.min(TaskOutcome.completed_at)))
    ).scalar_one_or_none()


async def delete_before(db: AsyncSession, cutoff: datetime) -> int:
    """Delete every outcome row strictly older than `cutoff`. Returns row
    count. Used by the prune handler — this is the only legal delete path
    on this table."""
    return (
        await db.execute(
            delete(TaskOutcome).where(TaskOutcome.completed_at < cutoff)
        )
    ).rowcount or 0


async def find_one_by_key(
    db: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
    object_id: str,
) -> TaskOutcome | None:
    """Single TaskOutcome row matching the composite key. Used by
    /tend/{run_id} to look up the dispatch row written when the run started."""
    return (
        await db.execute(
            select(TaskOutcome).where(
                TaskOutcome.task_kind == task_kind,
                TaskOutcome.object_kind == object_kind,
                TaskOutcome.object_id == object_id,
            )
        )
    ).scalar_one_or_none()


async def latest_completed_at_for(
    db: AsyncSession,
    *,
    task_kind: str,
    object_kind: str,
    object_id: str,
) -> datetime | None:
    """Most-recent completed_at for the given (kind, object) tuple, or None
    if it has never run. Used by periodic_tick to decide cadence."""
    return (
        await db.execute(
            select(func.max(TaskOutcome.completed_at)).where(
                TaskOutcome.task_kind == task_kind,
                TaskOutcome.object_kind == object_kind,
                TaskOutcome.object_id == object_id,
            )
        )
    ).scalar_one_or_none()
