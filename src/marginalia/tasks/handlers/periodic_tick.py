"""periodic_tick — the dispatcher (design.md §9.1 + §9.3).

This is the lowest-priority task in the system (priority 300). Its job each
firing:
  1. Walk PERIODIC_INTERVALS. For each (kind, interval):
     - if a pending/running row already exists for kind k, skip
     - otherwise look up the most recent done row's finished_at; if (now -
       finished_at) >= interval, enqueue(kind=k, dedup_key=k)
  2. Re-enqueue self (kind='periodic_tick') 10 minutes from now, with
     dedup_key='periodic_tick' to keep at most one in flight.

`recover_stuck_tasks` / `prune_audit_events` are dispatched through here —
they appear in PERIODIC_INTERVALS. The tick itself is NOT listed there; it
self-schedules so the chain never breaks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import select

from marginalia.db.models.tasks import Task
from marginalia.db.session import session_scope
from marginalia.services.audit import write_event
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import (
    KIND_PERIODIC_TICK,
    PERIODIC_INTERVALS,
    task_handler,
)

log = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 600  # 10 minutes


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; coerce to UTC-aware for arithmetic."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@task_handler(KIND_PERIODIC_TICK)
async def handle_periodic_tick(payload: Mapping[str, Any]) -> None:
    now = _utcnow()

    async with session_scope() as session:
        dispatched: list[str] = []
        skipped_recent: list[str] = []
        skipped_inflight: list[str] = []

        for kind, interval in PERIODIC_INTERVALS.items():
            in_flight = (
                await session.execute(
                    select(Task.id).where(
                        Task.kind == kind,
                        Task.status.in_(("pending", "running")),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if in_flight is not None:
                skipped_inflight.append(kind)
                continue

            last_done_at = _aware((
                await session.execute(
                    select(Task.finished_at)
                    .where(Task.kind == kind, Task.status == "done")
                    .order_by(Task.finished_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none())

            if last_done_at is not None and (now - last_done_at) < interval:
                skipped_recent.append(kind)
                continue

            task = await enqueue(
                session,
                kind=kind,
                payload={},
                dedup_key=kind,
            )
            if task is not None:
                dispatched.append(kind)
                await write_event(
                    session,
                    kind="task_enqueued",
                    task_id=task.id,
                    payload={"kind": kind, "scheduled_by": "periodic_tick"},
                )

        next_run = now + timedelta(seconds=TICK_INTERVAL_SECONDS)
        await enqueue(
            session,
            kind=KIND_PERIODIC_TICK,
            payload={},
            dedup_key=KIND_PERIODIC_TICK,
            scheduled_at=next_run,
        )

        await record_outcome(
            session,
            task_kind=KIND_PERIODIC_TICK,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if dispatched else "noop",
            detail={
                "dispatched": dispatched,
                "skipped_recent": skipped_recent,
                "skipped_inflight": skipped_inflight,
                "next_tick_at": next_run.isoformat(),
            },
        )
        await session.commit()


async def bootstrap_periodic_tick() -> None:
    """Ensure exactly one periodic_tick row exists at runner startup.

    Idempotent: if a pending/running tick already exists, no-op. Otherwise
    enqueue one due immediately so the dispatcher kicks in on the next claim.
    """
    async with session_scope() as session:
        existing = (
            await session.execute(
                select(Task.id).where(
                    Task.kind == KIND_PERIODIC_TICK,
                    Task.status.in_(("pending", "running")),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            await session.commit()
            return
        await enqueue(
            session,
            kind=KIND_PERIODIC_TICK,
            payload={"reason": "bootstrap"},
            dedup_key=KIND_PERIODIC_TICK,
        )
        await session.commit()
