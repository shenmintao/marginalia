"""Idempotent schema bootstrap — used by app startup and by 0001_initial.

`bootstrap_schema(bind)` creates every table defined on `Base.metadata` and
seeds the `_inbox` system catalog if absent. Called from:

  - `marginalia.main.lifespan` (FastAPI startup)
  - `marginalia.worker._arun` (worker daemon startup)
  - `alembic/versions/0001_initial.py` (when migrating from empty schema)

Re-runnable: `create_all` is a no-op when tables already exist; the inbox
seed uses `INSERT ... WHERE NOT EXISTS`. Column additions on existing
tables are handled by `_apply_additive_columns()` — we keep a small
hand-written list there because `create_all` only creates whole tables,
never adds missing columns to existing ones.
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from marginalia.db.engine import get_engine
from marginalia.db.models import Base  # noqa: F401  (registers all tables)
from marginalia.db.models.ai_structural import INBOX_CATALOG_ID


# Additive columns that landed after the v1 snapshot. Each entry:
# (table, column, ddl-fragment-after-name). Run ALTER TABLE ADD COLUMN
# only when the column is missing — both SQLite and Postgres support that.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "total_cache_read", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "total_cache_read", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "deleted_at", "TIMESTAMP NULL"),
)


def _apply_additive_columns(bind) -> None:
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table, column, ddl in _ADDITIVE_COLUMNS:
        if table not in existing_tables:
            continue
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column in cols:
            continue
        bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _relax_file_entries_folder_id_nullable(bind) -> None:
    """Make file_entries.folder_id nullable on existing SQLite DBs.

    SQLite has no `ALTER COLUMN DROP NOT NULL`, so we rebuild the table
    when (and only when) the live schema still says NOT NULL. No-op on
    Postgres (handled by alembic in a separate migration if/when needed)
    and on freshly-created SQLite tables (already nullable from the model).
    """
    if bind.dialect.name != "sqlite":
        return
    inspector = sa.inspect(bind)
    if "file_entries" not in inspector.get_table_names():
        return
    cols = {c["name"]: c for c in inspector.get_columns("file_entries")}
    fid = cols.get("folder_id")
    if fid is None or fid["nullable"]:
        return
    # Rebuild via Base.metadata.create_all on a renamed-old / new pattern.
    # `legacy_alter_table=ON` keeps SQLite from rewriting referencing FK
    # text in *other* tables when we RENAME — without it, every FK to
    # `file_entries` would be silently retargeted to `_file_entries_old`
    # and break the moment we drop the old table.
    bind.execute(sa.text("PRAGMA legacy_alter_table = ON"))
    bind.execute(sa.text("PRAGMA foreign_keys = OFF"))
    try:
        # SQLite carries indexes through RENAME (keeping their original names),
        # so they would collide with create()'s recreated indexes. Drop them
        # off the live table first; the renamed table doesn't need them.
        for idx in inspector.get_indexes("file_entries"):
            bind.execute(sa.text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
        bind.execute(sa.text("ALTER TABLE file_entries RENAME TO _file_entries_old"))
        Base.metadata.tables["file_entries"].create(bind=bind)
        bind.execute(sa.text(
            "INSERT INTO file_entries "
            "(id, folder_id, file_id, display_name, lifecycle, catalog_id, "
            " extra, deleted_at, purge_after, created_at, updated_at) "
            "SELECT id, NULLIF(folder_id, ''), file_id, display_name, lifecycle, "
            " catalog_id, extra, deleted_at, purge_after, created_at, updated_at "
            "FROM _file_entries_old"
        ))
        bind.execute(sa.text("DROP TABLE _file_entries_old"))
    finally:
        bind.execute(sa.text("PRAGMA foreign_keys = ON"))
        bind.execute(sa.text("PRAGMA legacy_alter_table = OFF"))


def _repair_dangling_file_entries_fks(bind) -> None:
    """One-shot repair: rebuild any table whose FK text still points at
    the now-deleted `_file_entries_old`. This was caused by an earlier
    bootstrap that renamed file_entries without `legacy_alter_table=ON`."""
    if bind.dialect.name != "sqlite":
        return
    rows = bind.execute(sa.text(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND sql LIKE '%_file_entries_old%'"
    )).fetchall()
    if not rows:
        return
    bind.execute(sa.text("PRAGMA legacy_alter_table = ON"))
    bind.execute(sa.text("PRAGMA foreign_keys = OFF"))
    try:
        for (name,) in rows:
            if name not in Base.metadata.tables:
                continue
            inspector = sa.inspect(bind)
            cols = [c["name"] for c in inspector.get_columns(name)]
            for idx in inspector.get_indexes(name):
                bind.execute(sa.text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
            bind.execute(sa.text(f'ALTER TABLE "{name}" RENAME TO "_{name}_old"'))
            Base.metadata.tables[name].create(bind=bind)
            col_list = ", ".join(f'"{c}"' for c in cols)
            bind.execute(sa.text(
                f'INSERT INTO "{name}" ({col_list}) '
                f'SELECT {col_list} FROM "_{name}_old"'
            ))
            bind.execute(sa.text(f'DROP TABLE "_{name}_old"'))
    finally:
        bind.execute(sa.text("PRAGMA foreign_keys = ON"))
        bind.execute(sa.text("PRAGMA legacy_alter_table = OFF"))


def _relax_sessions_end_reason_check(bind) -> None:
    """Extend `sessions.end_reason` CHECK to include newer enum values.

    SQLite has no `ALTER TABLE … DROP CONSTRAINT`, so when the live
    table's CHECK is older than what's defined in `enums.py` (e.g.
    missing `'deleted'`) we rebuild the table. No-op when the live
    constraint already matches the model, or on Postgres (handled by
    alembic if/when needed).
    """
    if bind.dialect.name != "sqlite":
        return
    inspector = sa.inspect(bind)
    if "sessions" not in inspector.get_table_names():
        return
    row = bind.execute(sa.text(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
    )).fetchone()
    if row is None:
        return
    live_sql = row[0] or ""
    # If every legal value already appears in the live CHECK text, nothing to do.
    from marginalia.db.models.enums import SESSION_END_REASONS
    missing = [v for v in SESSION_END_REASONS if f"'{v}'" not in live_sql]
    if not missing:
        return

    bind.execute(sa.text("PRAGMA legacy_alter_table = ON"))
    bind.execute(sa.text("PRAGMA foreign_keys = OFF"))
    try:
        cols = [c["name"] for c in inspector.get_columns("sessions")]
        for idx in inspector.get_indexes("sessions"):
            bind.execute(sa.text(f'DROP INDEX IF EXISTS "{idx["name"]}"'))
        bind.execute(sa.text("ALTER TABLE sessions RENAME TO _sessions_old"))
        Base.metadata.tables["sessions"].create(bind=bind)
        col_list = ", ".join(f'"{c}"' for c in cols)
        bind.execute(sa.text(
            f'INSERT INTO sessions ({col_list}) '
            f'SELECT {col_list} FROM _sessions_old'
        ))
        bind.execute(sa.text("DROP TABLE _sessions_old"))
    finally:
        bind.execute(sa.text("PRAGMA foreign_keys = ON"))
        bind.execute(sa.text("PRAGMA legacy_alter_table = OFF"))


def bootstrap_schema_sync(bind) -> None:
    """Synchronous variant — runs against a sync connection / engine.

    Used by Alembic migrations (which receive a sync bind from
    `op.get_bind()`) and by `bootstrap_schema()` below via `run_sync`.
    """
    Base.metadata.create_all(bind=bind)
    _apply_additive_columns(bind)
    _relax_file_entries_folder_id_nullable(bind)
    _repair_dangling_file_entries_fks(bind)
    _relax_sessions_end_reason_check(bind)
    now = datetime.now(timezone.utc).isoformat()
    bind.execute(
        sa.text(
            "INSERT INTO catalogs (id, parent_id, name, summary, description, "
            "extra, tags, is_system, deleted_at, created_at, updated_at) "
            "SELECT :id, NULL, :name, NULL, NULL, NULL, NULL, :is_system, "
            "NULL, :now, :now "
            "WHERE NOT EXISTS (SELECT 1 FROM catalogs WHERE id = :id)"
        ),
        {
            "id": INBOX_CATALOG_ID,
            "name": "_inbox",
            "is_system": True,
            "now": now,
        },
    )


async def bootstrap_schema() -> None:
    """Run schema creation + inbox seed against the configured async engine."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(bootstrap_schema_sync)
