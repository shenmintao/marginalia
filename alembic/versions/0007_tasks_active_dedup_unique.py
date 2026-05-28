"""tasks: enforce unique active dedup_key

Revision ID: 0007_tasks_active_dedup_unique
Revises: 0006_conversations_session_turn_unique
Create Date: 2026-05-28

Only pending/running rows are unique. Historical done/dead rows keep their
dedup_key for audit and scheduling introspection.
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _ensure_tasks_active_dedup_unique


revision = "0007_tasks_active_dedup_unique"
down_revision = "0006_conversations_session_turn_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_tasks_active_dedup_unique(op.get_bind())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_tasks_active_dedup_key")
