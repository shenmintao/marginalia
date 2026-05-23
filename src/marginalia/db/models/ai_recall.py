"""AI-internal recall layer: entry_relations, journal (design.md §8.3 — last 2).

Written by 🔍 investigator (reflect_turn only). The agent reads journal at
the start of each turn ("flip through my notebook") and reads entry_relations
implicitly as `related_entries` attached by read_entries_metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin


class EntryRelation(Base, IdMixin):
    """Pairwise structural association between entries.

    Construction enforces entry_a_id < entry_b_id (symmetric pair). One row per
    pair — repeat observations INCREMENT observation_count and update
    last_observed_at. There is NO controlled vocabulary for the relation kind:
    the `note` is free text that the agent reads and interprets at recall time.

    Ingest never writes here (single-file view can't reliably judge pairing).
    """

    __tablename__ = "entry_relations"
    __table_args__ = (
        UniqueConstraint("entry_a_id", "entry_b_id", name="uq_entry_relations_pair"),
        Index("ix_entry_relations_a", "entry_a_id"),
        Index("ix_entry_relations_b", "entry_b_id"),
        Index("ix_entry_relations_observation_count", "observation_count"),
    )

    entry_a_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_entries.id", ondelete="CASCADE"), nullable=False
    )
    entry_b_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_entries.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reflect")
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Journal(Base, IdMixin):
    """The investigator's pocket notebook (per-conversation, append-only).

    Written exclusively by reflect_turn. Each row is one self-contained note
    summarizing what was learned in that conversation, tied to the conversation
    that produced it. The agent's first move on a new turn is typically
    search_journal — "did I work on something like this before, how did it go?"
    """

    __tablename__ = "journal"
    __table_args__ = (
        Index("ix_journal_conversation_id", "conversation_id"),
        Index("ix_journal_created_at", "created_at"),
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    entry_ids: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    tags: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reflect_turn")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
