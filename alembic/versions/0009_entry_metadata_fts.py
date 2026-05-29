"""entry metadata fts5 index

Revision ID: 0009_entry_metadata_fts
Revises: 0008_query_performance_indexes
Create Date: 2026-05-29

Creates a SQLite FTS5 trigram index over metadata already stored in the DB:
entry display names/extras and file summaries/extras/extensions. Raw file
contents are not copied into SQLite by this migration.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import (
    _drop_entry_metadata_fts,
    _ensure_entry_metadata_fts,
)


revision = "0009_entry_metadata_fts"
down_revision = "0008_query_performance_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_entry_metadata_fts(op.get_bind())


def downgrade() -> None:
    _drop_entry_metadata_fts(op.get_bind())
