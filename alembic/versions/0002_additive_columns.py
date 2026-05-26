"""additive columns: total_cache_read on conversations + sessions, sessions.deleted_at

Revision ID: 0002_additive_columns
Revises: 0001_initial
Create Date: 2026-05-27

Wraps `_apply_additive_columns` from `marginalia.db.bootstrap`. The
helper is defensive: each ALTER runs only when the target column is
missing, so the revision is safe against DBs where bootstrap already
applied these mutations.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _apply_additive_columns


revision = "0002_additive_columns"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _apply_additive_columns(op.get_bind())


def downgrade() -> None:
    # SQLite has no DROP COLUMN, and Postgres rolling-back these would
    # take live data with it. Leave as a no-op — operators who really
    # need to undo should drop the columns by hand.
    pass
