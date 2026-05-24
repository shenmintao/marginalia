"""User-visible layer: folders, file_entries, files (DESIGN.md §8.1)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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
from marginalia.db.models.enums import (
    ENTRY_LIFECYCLES,
    FILE_KINDS,
    INGEST_STATUSES,
    _in_clause,
)


class Folder(Base, IdMixin, TimestampMixin):
    """User's virtual folder tree (Baidu-Netdisk style).

    Identity: written by user only. AI reads `name` as a soft prior signal at
    ingest, but never writes here.
    """

    __tablename__ = "folders"
    __table_args__ = (UniqueConstraint("parent_id", "name", name="uq_folders_parent_name"),)

    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("folders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FileEntry(Base, IdMixin, TimestampMixin):
    """User's reference to a physical file inside a folder, with per-entry AI fields.

    `catalog_id` and `extra` are per-position AI fields: same sha256 in different
    folders may carry different classification / insight. On dedup, both are
    seeded by copying from a source entry then evolve independently.
    """

    __tablename__ = "file_entries"
    __table_args__ = (
        Index("ix_file_entries_folder_id", "folder_id"),
        Index("ix_file_entries_file_id", "file_id"),
        Index("ix_file_entries_lifecycle", "lifecycle"),
        Index("ix_file_entries_catalog_id", "catalog_id"),
        CheckConstraint(_in_clause("lifecycle", ENTRY_LIFECYCLES), name="lifecycle"),
    )

    folder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("folders.id", ondelete="RESTRICT"), nullable=False
    )
    file_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("files.id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # active | demoted | archived | manual_active | manual_archived
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    catalog_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("catalogs.id", ondelete="SET NULL"), nullable=True
    )
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class File(Base, IdMixin, TimestampMixin):
    """Physical file (content-addressed, write-once content fields).

    `summary` / `description` / `extra` / `kind` describe the immutable byte
    stream itself. They are written exactly once by the ingest_file task and
    locked by `ingested_at`. Service-layer code MUST refuse updates when
    `ingested_at IS NOT NULL`.
    """

    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_ingest_status", "ingest_status"),
        Index("ix_files_kind", "kind"),
        CheckConstraint(_in_clause("ingest_status", INGEST_STATUSES), name="ingest_status"),
        CheckConstraint(
            f"kind IS NULL OR {_in_clause('kind', FILE_KINDS)}",
            name="kind",
        ),
    )

    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # NOT unique: mirror backend has dedup OFF and intentionally creates
    # multiple file rows with the same sha256 (one per upload, even of
    # the same bytes to different folders). Local backend still enforces
    # uniqueness implicitly via its dedup logic in services/upload.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    original_ext: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # text | table | log | image | audio | video | code | container
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[Any | None] = mapped_column(JSON, nullable=True)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    # pending | processing | done | failed
    ingest_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
