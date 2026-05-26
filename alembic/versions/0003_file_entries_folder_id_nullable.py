"""file_entries.folder_id: drop NOT NULL on existing SQLite DBs

Revision ID: 0003_file_entries_folder_id_nullable
Revises: 0002_additive_columns
Create Date: 2026-05-27

Wraps `_relax_file_entries_folder_id_nullable`. SQLite-only: the helper
rebuilds the table when the live schema still has NOT NULL on
folder_id; on Postgres it's a no-op (operators drop the constraint via
a hand-written migration when needed).
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _relax_file_entries_folder_id_nullable


revision = "0003_file_entries_folder_id_nullable"
down_revision = "0002_additive_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _relax_file_entries_folder_id_nullable(op.get_bind())


def downgrade() -> None:
    pass
