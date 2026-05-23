"""Audit layer: audit_events, sessions, conversations (design.md §8.2).

Shared infrastructure tables. Agent NEVER reads these — AI's "past experience"
flows through the journal table. Humans read these via admin tooling; the
runtime reads sessions/conversations to maintain rolling counters.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin


class AuditEvent(Base, IdMixin):
    """Database-change event stream (90-day rolling).

    Records every state-changing action against the DB. INSERT-only —
    `prune_audit_events` is the sole delete path.

    `kind` examples: file_created / entry_created / lifecycle_changed /
    journal_entry_written / tag_created / tag_merged / catalog_moved /
    task_started / task_finished / ingest_status_changed / ...

    Does NOT record in-memory tool_call / llm_call events — those live inside
    `conversations.tool_calls` / `conversations.llm_calls` JSON columns.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_session_occurred", "session_id", "occurred_at"),
        Index("ix_audit_events_conversation_occurred", "conversation_id", "occurred_at"),
        Index("ix_audit_events_task_occurred", "task_id", "occurred_at"),
        Index("ix_audit_events_kind_occurred", "kind", "occurred_at"),
    )

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)


class Session(Base, IdMixin):
    """A use-window container.

    end_reason taxonomy:
      - cleared : user explicitly issued /clear
      - normal  : caller exited gracefully
      - unclean : process crash / lease expired (recover_stuck_tasks marks)
    """

    __tablename__ = "sessions"

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    initiating_user_message: Mapped[str] = mapped_column(Text, nullable=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cache_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Conversation(Base, IdMixin):
    """One turn of activity inside a session.

    Reading `conversations` rows in time order reproduces the agent's full
    workflow that turn. The agent NEVER reads this table — its memory of past
    work flows through `journal`.

    `tool_calls` / `llm_calls` are JSON arrays appended in real time.
    A plan is just another conversation — same shape as a user message or an
    agent reply — so there is no dedicated `plan` column.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_session_turn", "session_id", "turn_index"),
        Index("ix_conversations_started_at", "started_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    agent_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    llm_calls: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
