"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-23

The actual table-creation + inbox-seed logic lives in
`marginalia.db.bootstrap.bootstrap_baseline_sync` so the application
startup path and the migration path use exactly the same code.

This revision intentionally does NOT include any of the post-baseline
shims (`_apply_additive_columns`, `_relax_*`, …) — each of those landed
later and gets its own revision (0002+).
"""
from __future__ import annotations

from alembic import op

from marginalia.db.bootstrap import bootstrap_baseline_sync
from marginalia.db.models import Base  # noqa: F401


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bootstrap_baseline_sync(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
