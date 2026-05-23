"""task_outcomes service — design.md §8.4 + §14.3.

Tiny wrapper for INSERT and recency / idempotence queries against the
`task_outcomes` table. Caller controls the transaction (matches services.audit
shape).

Conventions:
  - object_kind / object_id strings are documented per task in the handler
    module top-of-file. Use 'global' / 'global' for tasks that don't operate
    on a single object (normalize_tags, prune_*, etc.).
  - outcome ∈ {'applied', 'noop', 'rejected', 'deferred'}.
  - detail is task-specific JSON. Keep keys consistent across handlers so a
    single ad-hoc query can pull the whole story for an object.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import TaskOutcome
from marginalia.utils.ids import new_id


VALID_OUTCOMES = ("applied", "noop", "rejected", "deferred")
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
