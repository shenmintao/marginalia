"""include file descriptions in entry metadata fts

Revision ID: 0010_entry_metadata_fts_description
Revises: 0009_entry_metadata_fts
Create Date: 2026-05-30

Rebuilds the SQLite metadata FTS table so description.sections and other
file.description JSON text participate in deterministic metadata search.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import (
    _drop_entry_metadata_fts,
    _ensure_entry_metadata_fts_description,
)


revision = "0010_entry_metadata_fts_description"
down_revision = "0009_entry_metadata_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_entry_metadata_fts_description(op.get_bind())


def downgrade() -> None:
    _drop_entry_metadata_fts(op.get_bind())
