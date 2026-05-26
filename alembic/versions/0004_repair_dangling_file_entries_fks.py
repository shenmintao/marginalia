"""repair tables whose FK text still points at _file_entries_old

Revision ID: 0004_repair_dangling_file_entries_fks
Revises: 0003_file_entries_folder_id_nullable
Create Date: 2026-05-27

One-shot repair from an earlier bootstrap that renamed file_entries
without `legacy_alter_table=ON`. SQLite-only; no-op when nothing dangles.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _repair_dangling_file_entries_fks


revision = "0004_repair_dangling_file_entries_fks"
down_revision = "0003_file_entries_folder_id_nullable"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _repair_dangling_file_entries_fks(op.get_bind())


def downgrade() -> None:
    pass
