"""prune_task_outcomes — design.md §8.4 + §14.2.3a.

The only legal delete path on `task_outcomes`. Default retention = 30 days
(covers the longest periodic interval, suggest_archival = 14d, with double
buffer). One INSERT-only summary row is added at the end so the prune itself
leaves a trace in the same table.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import delete, func, select

from marginalia.db.models import TaskOutcome
from marginalia.db.session import session_scope
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_PRUNE_TASK_OUTCOMES, task_handler

log = logging.getLogger(__name__)

RETENTION = timedelta(days=30)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_PRUNE_TASK_OUTCOMES)
async def handle_prune_task_outcomes(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    retention_days = int(payload.get("retention_days") or RETENTION.days)
    cutoff = now - timedelta(days=retention_days)

    async with session_scope() as session:
        oldest = (
            await session.execute(select(func.min(TaskOutcome.completed_at)))
        ).scalar_one_or_none()
        deleted = (
            await session.execute(
                delete(TaskOutcome).where(TaskOutcome.completed_at < cutoff)
            )
        ).rowcount or 0

        await record_outcome(
            session,
            task_kind=KIND_PRUNE_TASK_OUTCOMES,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied",
            detail={
                "deleted": deleted,
                "cutoff": cutoff.isoformat(),
                "retention_days": retention_days,
                "oldest_before": oldest.isoformat() if oldest else None,
            },
        )
        log.info(
            "prune_task_outcomes: deleted=%d (cutoff=%s)", deleted, cutoff.isoformat()
        )
        await session.commit()
