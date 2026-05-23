"""AI-internal structural layer: catalogs, views, tags, tag_aliases, entry_tags
(design.md §8.3 — first 5 tables).

Written by 🏛️ librarian (offline tasks). User layer NEVER reads these.
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

from marginalia.db.models.base import Base, IdMixin, TimestampMixin


class Catalog(Base, IdMixin, TimestampMixin):
    """AI's classification tree (single-parent tree; AI-curated, user-invisible).

    `extra` is mutable current-understanding (overwritten by reflect_turn /
    restructure_catalogs). `summary` / `description` / `tags` describe the
    node itself.
    """

    __tablename__ = "catalogs"
    __table_args__ = (Index("ix_catalogs_parent_id", "parent_id"),)

    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("catalogs.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class View(Base, IdMixin, TimestampMixin):
    """Topic-aggregating view across catalogs.

    `filter_spec` is a structured filter (catalog_subtree, tags_all/any/none,
    facets, date_range). Materialized on-demand by the `materialize_view` tool.
    All views are AI-created in V1 (no user creation entry-point).
    """

    __tablename__ = "views"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    filter_spec: Mapped[Any] = mapped_column(JSON, nullable=False, default=dict)


class Tag(Base, IdMixin, TimestampMixin):
    """Controlled vocabulary (emerges post-hoc via normalize_tags).

    facet ∈ {topic, form, time, source, language, extra}.
    `alias_of` points at the canonical tag (which must itself have alias_of=NULL —
    no chained aliases; normalize_tags maintains this invariant).
    """

    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("name", "facet", name="uq_tags_name_facet"),
        Index("ix_tags_facet", "facet"),
        Index("ix_tags_alias_of", "alias_of"),
    )

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    facet: Mapped[str] = mapped_column(String(16), nullable=False)
    alias_of: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tags.id", ondelete="RESTRICT"), nullable=True
    )
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TagAlias(Base, IdMixin):
    """Authority file: any spelling -> canonical tag id.

    Never deleted — historical merges are facts. resolve_tag(name) hits
    tags.name first, then falls back to tag_aliases.from_name.
    """

    __tablename__ = "tag_aliases"
    __table_args__ = (Index("ix_tag_aliases_from_name", "from_name"),)

    from_name: Mapped[str] = mapped_column(String(255), nullable=False)
    to_tag_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EntryTag(Base):
    """entry <-> tag association with provenance.

    Composite PK (entry_id, tag_id). `source` records HOW the tag was attached
    (ingest / reflect / enrich_tags / dedup_seed). normalize_tags rewrites
    rows when merging duplicate tags.
    """

    __tablename__ = "entry_tags"
    __table_args__ = (
        Index("ix_entry_tags_tag_id", "tag_id"),
        Index("ix_entry_tags_source", "source"),
    )

    entry_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("file_entries.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tag_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tags.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
