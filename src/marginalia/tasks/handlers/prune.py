"""prune — unified retention pruner for audit_events + task_outcomes.

design.md §9.1 + §14.2.3 + §14.2.3a.

Two retention windows live in this one handler:
  - audit_events       : 90d (the audit log)
  - task_outcomes      : 30d (covers longest periodic = suggest_archival 14d × 2)

Both are INSERT-only tables, and this handler is their sole legal delete path.
After deleting, ONE summary row is written into task_outcomes covering the
whole run (per-target counts in the detail JSON). audit_events also gets one
`audit_events_pruned` row per phase that actually deleted anything, so the
prune itself stays auditable.

Payload (all optional):
  {"targets": ["audit_events", "task_outcomes"]}  # default: both
  {"retention_days": {"audit_events": 90, "task_outcomes": 30}}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import delete, func, select

from marginalia.db.models import AuditEvent, TaskOutcome
from marginalia.db.session import session_scope
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_PRUNE, task_handler

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS: Mapping[str, int] = {
    "audit_events": 90,
    "task_outcomes": 30,
}
ALL_TARGETS = ("audit_events", "task_outcomes")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_PRUNE)
async def handle_prune(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    targets = list(payload.get("targets") or ALL_TARGETS)
    retention_days = dict(DEFAULT_RETENTION_DAYS)
    retention_days.update(dict(payload.get("retention_days") or {}))

    per_target: dict[str, dict[str, Any]] = {}
    total_deleted = 0

    async with session_scope() as session:
        for target in targets:
            days = int(retention_days.get(target, 0))
            if days <= 0:
                continue
            cutoff = now - timedelta(days=days)
            if target == "audit_events":
                deleted, oldest = await _prune_audit_events(session, cutoff)
            elif target == "task_outcomes":
                deleted, oldest = await _prune_task_outcomes(session, cutoff)
            else:
                log.warning("prune: unknown target %r — skipped", target)
                continue
            total_deleted += deleted
            per_target[target] = {
                "deleted": deleted,
                "cutoff": cutoff.isoformat(),
                "retention_days": days,
                "oldest_before": oldest.isoformat() if oldest else None,
            }

        await record_outcome(
            session,
            task_kind=KIND_PRUNE,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if total_deleted else "noop",
            detail={"per_target": per_target, "total_deleted": total_deleted},
        )
        log.info(
            "prune: total_deleted=%d targets=%s",
            total_deleted, list(per_target.keys()),
        )
        await session.commit()


async def _prune_audit_events(session, cutoff: datetime) -> tuple[int, datetime | None]:
    oldest = (
        await session.execute(select(func.min(AuditEvent.occurred_at)))
    ).scalar_one_or_none()
    deleted = (
        await session.execute(
            delete(AuditEvent).where(AuditEvent.occurred_at < cutoff)
        )
    ).rowcount or 0
    if deleted:
        await AuditEvent.append(
            session,
            kind="audit_events_pruned",
            payload={"deleted": deleted, "cutoff": cutoff.isoformat()},
        )
    return deleted, oldest


async def _prune_task_outcomes(session, cutoff: datetime) -> tuple[int, datetime | None]:
    oldest = (
        await session.execute(select(func.min(TaskOutcome.completed_at)))
    ).scalar_one_or_none()
    deleted = (
        await session.execute(
            delete(TaskOutcome).where(TaskOutcome.completed_at < cutoff)
        )
    ).rowcount or 0
    return deleted, oldest
