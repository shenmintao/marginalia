"""Audit layer: audit_events, sessions, conversations (DESIGN.md §8.2).

Shared infrastructure tables. Agent NEVER reads these — AI's "past experience"
flows through the journal table. Humans read these via admin tooling; the
runtime reads sessions/conversations to maintain rolling counters.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin, UtcDateTime
from marginalia.db.models.enums import SESSION_END_REASONS, _in_clause


class AuditEvent(Base, IdMixin):
    """Database-change event stream (90-day rolling).

    Records every state-changing action against the DB. INSERT-only —
    `prune` is the sole delete path. Use `repositories.audit_events.append`
    to write rows.

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

    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
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
      - deleted : closed implicitly by a UI delete (the GUI never calls
                  /close, so the trash icon doubles as "stop this session")
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            f"end_reason IS NULL OR {_in_clause('end_reason', SESSION_END_REASONS)}",
            name="end_reason",
        ),
    )

    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, default=None,
    )
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
        # Unique-constrained covering index: serves both the
        # `WHERE session_id=? ORDER BY turn_index` lookups and the
        # data-integrity invariant. Without this, two concurrent
        # `run_turn` calls for the same session race on the
        # `latest_turn_index() + 1` read-modify-write and silently
        # write two rows with the same turn_index. The route layer
        # also serialises with a per-session asyncio.Lock; this
        # constraint is the database-level backstop that survives
        # multi-process deploys (Postgres + multiple workers).
        UniqueConstraint(
            "session_id", "turn_index",
            name="uq_conversations_session_turn",
        ),
        Index("ix_conversations_started_at", "started_at"),
    )

    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    user_message: Mapped[str] = mapped_column(Text, nullable=False)
    agent_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    llm_calls: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    total_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cache_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_estimate: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
