"""task_outcomes — design.md §8.4.

Scheduling-side fact table. Records "what did task X do to object Y when?"
so handlers can answer idempotence / recency questions WITHOUT reading
audit_events. Read by infrastructure only; INSERT-only; pruned on a 30-day
rolling window by `prune` (task_outcomes target).

Why a separate table instead of audit_events:
  - audit_events is the "data-change event stream" — read by humans for
    auditing. Mixing scheduling state (entry_enriched, reflect_turn_completed,
    etc.) into it pollutes the human-readable audit and forces JSON-path
    queries the index can't serve.
  - task_outcomes has dedicated indexes on (task_kind, object_kind, object_id)
    and (task_kind, completed_at) — exactly the access patterns scheduling
    needs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin


class TaskOutcome(Base, IdMixin):
    """One row = one (task, object) processing record.

    `object_kind='global'` + `object_id='global'` is used by tasks that
    don't operate on a single object (e.g. `normalize_tags` runs across
    every facet; `prune` cleans the whole audit table).
    """

    __tablename__ = "task_outcomes"
    __table_args__ = (
        Index("ix_task_outcomes_lookup", "task_kind", "object_kind", "object_id"),
        Index("ix_task_outcomes_recency", "task_kind", "completed_at"),
        Index("ix_task_outcomes_completed_at", "completed_at"),
    )

    task_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    object_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)
    task_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
