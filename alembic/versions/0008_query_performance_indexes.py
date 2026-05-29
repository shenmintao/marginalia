"""query performance indexes

Revision ID: 0008_query_performance_indexes
Revises: 0007_tasks_active_dedup_unique
Create Date: 2026-05-29

Adds composite indexes that match the repository query shapes used by the
SQLite desktop path: live folder/catalog lists, task queue claims, recent
journal/session reads, and task_outcome recency checks.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import (
    QUERY_PERFORMANCE_INDEXES,
    _ensure_query_performance_indexes,
)


revision = "0008_query_performance_indexes"
down_revision = "0007_tasks_active_dedup_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_query_performance_indexes(op.get_bind())


def downgrade() -> None:
    for index_name, _table_name, _columns in reversed(QUERY_PERFORMANCE_INDEXES):
        op.execute(f'DROP INDEX IF EXISTS "{index_name}"')
