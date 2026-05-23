"""Minimal audit-event writer (design.md §14.2.6).

Every state-changing DB operation must INSERT a matching `audit_events` row in
the SAME transaction, so the audit log can never disagree with reality. This
module is deliberately a thin wrapper — no policy, no retention, no fanout —
so callers can compose it into any service without coupling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import AuditEvent
from marginalia.utils.ids import new_id


async def write_event(
    session: AsyncSession,
    *,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    task_id: str | None = None,
    occurred_at: datetime | None = None,
) -> AuditEvent:
    """Append one audit_events row. Caller controls the transaction."""
    event = AuditEvent(
        id=new_id(),
        occurred_at=occurred_at or datetime.now(timezone.utc),
        kind=kind,
        session_id=session_id,
        conversation_id=conversation_id,
        task_id=task_id,
        payload=dict(payload or {}),
    )
    session.add(event)
    return event
