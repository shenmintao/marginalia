"""prune_audit_events — design.md §9.1 + §14.2.3.

audit_events is the only audit log; it is INSERT-only. This handler is the
sole legal delete path. Default retention = 90 days. Each invocation deletes
all rows with `occurred_at < now - 90d`, then writes ONE summary audit event
(`audit_events_pruned`) so the act of pruning is itself audited.

Note: the new audit row's occurred_at = now, so it cannot be deleted by this
same invocation. The next invocation 24h later will not delete it either
(unless retention is shrunk well below 1 day).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import delete, func, select

from marginalia.db.models import AuditEvent
from marginalia.db.session import session_scope
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_PRUNE_AUDIT_EVENTS, task_handler

log = logging.getLogger(__name__)

RETENTION = timedelta(days=90)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_PRUNE_AUDIT_EVENTS)
async def handle_prune_audit_events(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    retention_days = int(payload.get("retention_days") or RETENTION.days)
    cutoff = now - timedelta(days=retention_days)

    async with session_scope() as session:
        oldest = (
            await session.execute(select(func.min(AuditEvent.occurred_at)))
        ).scalar_one_or_none()
        deleted = (
            await session.execute(
                delete(AuditEvent).where(AuditEvent.occurred_at < cutoff)
            )
        ).rowcount or 0

        await record_outcome(
            session,
            task_kind=KIND_PRUNE_AUDIT_EVENTS,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if deleted else "noop",
            detail={
                "deleted": deleted,
                "cutoff": cutoff.isoformat(),
                "retention_days": retention_days,
                "oldest_before": oldest.isoformat() if oldest else None,
            },
        )
        log.info("audit_events_pruned: deleted=%d (cutoff=%s)", deleted, cutoff.isoformat())
        await session.commit()
