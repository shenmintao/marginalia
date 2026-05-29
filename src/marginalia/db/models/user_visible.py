"""User-visible layer: folders, file_entries, files (DESIGN.md §8.1)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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

from marginalia.db.models.base import Base, IdMixin, TimestampMixin, UtcDateTime
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
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_folders_parent_name"),
        Index("ix_folders_parent_live_name", "parent_id", "deleted_at", "name"),
    )

    parent_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("folders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


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
        Index("ix_file_entries_folder_live_name", "folder_id", "deleted_at", "display_name"),
        Index("ix_file_entries_file_live_created", "file_id", "deleted_at", "created_at"),
        Index("ix_file_entries_catalog_live_updated", "catalog_id", "deleted_at", "updated_at"),
        Index("ix_file_entries_lifecycle_live_created", "lifecycle", "deleted_at", "created_at"),
        Index("ix_file_entries_lifecycle_live_updated", "lifecycle", "deleted_at", "updated_at"),
        Index("ix_file_entries_deleted_purge", "deleted_at", "purge_after"),
        CheckConstraint(_in_clause("lifecycle", ENTRY_LIFECYCLES), name="lifecycle"),
    )

    folder_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("folders.id", ondelete="RESTRICT"), nullable=True,
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
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    purge_after: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


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
        Index("ix_files_live_created", "deleted_at", "created_at"),
        Index("ix_files_live_ingested", "deleted_at", "ingested_at"),
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
    ingested_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
