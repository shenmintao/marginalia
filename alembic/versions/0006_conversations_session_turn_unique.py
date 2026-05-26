"""conversations: replace ix_conversations_session_turn with UNIQUE(session_id, turn_index)

Revision ID: 0006_conversations_session_turn_unique
Revises: 0005_sessions_end_reason_check
Create Date: 2026-05-27

Wraps `_ensure_conversations_session_turn_unique`. The route layer
serialises turns per-session with an asyncio.Lock; this index is the
cross-process backstop. The helper raises if existing rows already
violate the invariant — operator decides what to do (usually: delete
the duplicate by id).
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import _ensure_conversations_session_turn_unique


revision = "0006_conversations_session_turn_unique"
down_revision = "0005_sessions_end_reason_check"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_conversations_session_turn_unique(op.get_bind())


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_conversations_session_turn")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_session_turn "
        "ON conversations (session_id, turn_index)"
    )
