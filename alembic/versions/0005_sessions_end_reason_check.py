"""sessions.end_reason: extend CHECK to include newer enum values

Revision ID: 0005_sessions_end_reason_check
Revises: 0004_repair_dangling_file_entries_fks
Create Date: 2026-05-27

Wraps `_relax_sessions_end_reason_check`. SQLite has no
`ALTER TABLE … DROP CONSTRAINT`, so the helper rebuilds `sessions`
when the live CHECK is older than `enums.SESSION_END_REASONS`.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _relax_sessions_end_reason_check


revision = "0005_sessions_end_reason_check"
down_revision = "0004_repair_dangling_file_entries_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _relax_sessions_end_reason_check(op.get_bind())


def downgrade() -> None:
    pass
