"""AI-internal recall layer: entry_relations, journal (DESIGN.md §8.3 — last 2).

Written by 🔍 investigator (reflect_turn only). The agent reads journal at
the start of each turn ("flip through my notebook") and reads entry_relations
implicitly as `related_entries` attached by read_entries_metadata.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from marginalia.db.models.base import Base, IdMixin, UtcDateTime
from marginalia.db.models.enums import (
    ENTRY_RELATION_SOURCE_KINDS,
    JOURNAL_SOURCE_KINDS,
    _in_clause,
)


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
        CheckConstraint(
            _in_clause("source_kind", ENTRY_RELATION_SOURCE_KINDS),
            name="source_kind",
        ),
    )

    entry_a_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_entries.id", ondelete="CASCADE"), nullable=False
    )
    entry_b_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_entries.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # vetted: NULL = not yet judged by vet_relations; True/False = LLM verdict.
    # vetted_observation_count: snapshot of observation_count at vet time;
    # used to decide when a vetted edge needs revisiting (when current count
    # grows substantially beyond the snapshot).
    vetted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    vetted_reason: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    vetted_at: Mapped[datetime | None] = mapped_column(
        UtcDateTime(), nullable=True, default=None,
    )
    vetted_observation_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class Journal(Base, IdMixin):
    """The investigator's notebook — two tiers in one table.

    Two source_kind values share this table (see [[journal-tiers]]):

    - `reflect_turn`: per-conversation bullet, written by reflect_turn after
      each finished turn. Per-session view of "what happened this turn".
    - `insight`: cross-session distillation, written by summarize_session
      after a session has accumulated ≥K reflect_turn rows. Long-lived
      "what should the next session know" notes.

    Both rows carry conversation_id (NOT NULL): for reflect_turn it is the
    turn that produced the bullet; for insight it is the LAST conversation
    of the session that the insight summarizes — useful for tracing the
    insight back to its raw bullets via session_id.

    `superseded_by_id` (insight only) chains evolution: when a later
    summarize_session run produces an insight that supersedes an earlier
    one (e.g. user changed their mind about a routing rule), the older row
    points forward to the newer. Active-insight queries filter
    `WHERE superseded_by_id IS NULL`.

    `summarized_journal_ids` (insight only) is the list of reflect_turn
    journal ids that summarize_session distilled into this insight —
    answers "which raw bullets did this insight come from?" for audit
    and debugging. NULL on reflect_turn rows.
    """

    __tablename__ = "journal"
    __table_args__ = (
        Index("ix_journal_conversation_id", "conversation_id"),
        Index("ix_journal_created_at", "created_at"),
        Index("ix_journal_source_kind", "source_kind"),
        CheckConstraint(
            _in_clause("source_kind", JOURNAL_SOURCE_KINDS),
            name="source_kind",
        ),
        CheckConstraint(
            "source_kind = 'insight' OR superseded_by_id IS NULL",
            name="supersede_only_on_insight",
        ),
        CheckConstraint(
            "source_kind = 'insight' OR summarized_journal_ids IS NULL",
            name="summarized_only_on_insight",
        ),
    )

    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str] = mapped_column(Text, nullable=False)
    entry_ids: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    tags: Mapped[Any] = mapped_column(JSON, nullable=False, default=list)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="reflect_turn")
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("journal.id", ondelete="SET NULL"),
        nullable=True,
        default=None,
    )
    summarized_journal_ids: Mapped[Any | None] = mapped_column(
        JSON, nullable=True, default=None,
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
