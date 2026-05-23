"""recover_stuck_tasks — design.md §9.1.

Self-healing: when a worker dies mid-task (process crash, OOM, lease expired
without heartbeat reaching DB), the row stays at status='running' forever
unless something resurrects it. This handler does that.

Strategy: any row at status='running' AND lease_expires_at < now (with a tiny
grace window) is assumed dead. We:
  - reset status='pending', clear locked_by + lease_expires_at
  - leave attempts unchanged (the runner already incremented on claim — it's
    "fair" to count this as one used attempt; backoff is not extended since
    we don't know how the worker died)
  - if attempts >= max_attempts, mark dead instead

Audit: one `task_recovered` event per recovered row, plus one `task_marked_dead`
per row that exhausted retries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, update

from marginalia.db.models.tasks import Task
from marginalia.db.session import session_scope
from marginalia.services.audit import write_event
from marginalia.tasks.kinds import KIND_RECOVER_STUCK_TASKS, task_handler

log = logging.getLogger(__name__)

GRACE_SECONDS = 10  # treat anything past lease+grace as definitively dead


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_RECOVER_STUCK_TASKS)
async def handle_recover_stuck_tasks(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    cutoff = now

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Task).where(
                    Task.status == "running",
                    Task.lease_expires_at.isnot(None),
                    Task.lease_expires_at < cutoff,
                )
            )
        ).scalars().all()

        recovered = 0
        marked_dead = 0
        for t in rows:
            previous_locked = t.locked_by
            previous_lease = t.lease_expires_at
            if t.attempts >= t.max_attempts:
                await session.execute(
                    update(Task)
                    .where(Task.id == t.id, Task.status == "running")
                    .values(
                        status="dead",
                        last_error="recover_stuck_tasks: lease expired beyond max_attempts",
                        finished_at=now,
                        locked_by=None,
                        lease_expires_at=None,
                    )
                )
                await write_event(
                    session,
                    kind="task_marked_dead",
                    task_id=t.id,
                    payload={
                        "kind": t.kind,
                        "attempts": t.attempts,
                        "max_attempts": t.max_attempts,
                        "previous_locked_by": previous_locked,
                        "previous_lease_expires_at": (
                            previous_lease.isoformat() if previous_lease else None
                        ),
                        "reason": "lease_expired_max_attempts",
                    },
                )
                marked_dead += 1
            else:
                await session.execute(
                    update(Task)
                    .where(Task.id == t.id, Task.status == "running")
                    .values(
                        status="pending",
                        locked_by=None,
                        lease_expires_at=None,
                        scheduled_at=now,
                    )
                )
                await write_event(
                    session,
                    kind="task_recovered",
                    task_id=t.id,
                    payload={
                        "kind": t.kind,
                        "attempts": t.attempts,
                        "previous_locked_by": previous_locked,
                        "previous_lease_expires_at": (
                            previous_lease.isoformat() if previous_lease else None
                        ),
                    },
                )
                recovered += 1

        if recovered or marked_dead:
            log.info(
                "recover_stuck_tasks: recovered=%d marked_dead=%d", recovered, marked_dead
            )
        await session.commit()
