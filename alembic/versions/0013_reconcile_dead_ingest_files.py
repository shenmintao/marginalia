"""reconcile files left active after dead ingest tasks

Revision ID: 0013_reconcile_dead_ingest_files
Revises: 0012_journal_invalidation
Create Date: 2026-06-19

Older workers could mark an ingest_file task dead after an exception or stale
lease without mirroring that terminal state to files.ingest_status. This data
repair is idempotent and only touches files that have no active ingest task.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _reconcile_dead_ingest_files


revision = "0013_reconcile_dead_ingest_files"
down_revision = "0012_journal_invalidation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _reconcile_dead_ingest_files(op.get_bind())


def downgrade() -> None:
    pass
