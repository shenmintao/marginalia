from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin


class Task(Base, IdMixin):
    """Unified async task queue.

    Every async unit of work in Marginalia goes through this one table:
    file ingest, post-conversation reflection, offline batch normalization,
    catalog restructuring, co-occurrence computation, GC. No external broker.
    """

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_status_sched_priority", "status", "scheduled_at", "priority"),
        Index("ix_tasks_dedup_key_active", "dedup_key"),
        Index("ix_tasks_kind_status", "kind", "status"),
    )

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)
    dedup_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
