"""Idempotent schema bootstrap — used by app startup and by alembic.

Two entry points:

* `bootstrap_schema()` — async, called from FastAPI / worker startup. Runs
  the baseline (`create_all` + inbox seed), every cumulative shim, then
  stamps `alembic_version` to head so `alembic upgrade head` becomes a
  no-op against this DB.

* `bootstrap_baseline_sync(bind)` — synchronous, called from
  `alembic/versions/0001_initial.py`. Just `create_all` + inbox seed,
  without any of the post-v1 shims (those each get their own revision so
  production deploys can apply them surgically and the alembic history is
  honest about when each invariant landed).

The individual shim functions (`_apply_additive_columns`,
`_relax_file_entries_folder_id_nullable`, …) are imported by their
matching `0002..N` alembic revisions. They stay here rather than getting
inlined into the revision files because they double as "make this DB
match the current model on first boot" for fresh dev installs that have
never seen alembic.

Re-runnable: every helper checks its precondition before mutating, so
running the whole pipeline twice is safe — that's what makes the dev
loop survive without per-step migrations.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

import sqlalchemy as sa

from marginalia.db.engine import get_engine
from marginalia.db.fts import ENTRY_METADATA_FTS_TABLE, ENTRY_METADATA_FTS_TRIGGERS
from marginalia.db.models import Base  # noqa: F401  (registers all tables)
from marginalia.db.models.ai_structural import INBOX_CATALOG_ID


# Additive columns that landed after the v1 snapshot. Each entry:
# (table, column, ddl-fragment-after-name). Run ALTER TABLE ADD COLUMN
# only when the column is missing — both SQLite and Postgres support that.
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("conversations", "total_cache_read", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "total_cache_read", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "deleted_at", "TIMESTAMP NULL"),
    ("journal", "invalidated_at", "TIMESTAMP NULL"),
    ("journal", "invalidated_by_id", "VARCHAR(36) NULL"),
    ("journal", "invalidated_reason", "TEXT NULL"),
)


QUERY_PERFORMANCE_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_folders_parent_live_name",
        "folders",
        ("parent_id", "deleted_at", "name"),
    ),
    (
        "ix_file_entries_folder_live_name",
        "file_entries",
        ("folder_id", "deleted_at", "display_name"),
    ),
    (
        "ix_file_entries_file_live_created",
        "file_entries",
        ("file_id", "deleted_at", "created_at"),
    ),
    (
        "ix_file_entries_catalog_live_updated",
        "file_entries",
        ("catalog_id", "deleted_at", "updated_at"),
    ),
    (
        "ix_file_entries_lifecycle_live_created",
        "file_entries",
        ("lifecycle", "deleted_at", "created_at"),
    ),
    (
        "ix_file_entries_lifecycle_live_updated",
        "file_entries",
        ("lifecycle", "deleted_at", "updated_at"),
    ),
    (
        "ix_file_entries_deleted_purge",
        "file_entries",
        ("deleted_at", "purge_after"),
    ),
    (
        "ix_files_live_created",
        "files",
        ("deleted_at", "created_at"),
    ),
    (
        "ix_files_live_ingested",
        "files",
        ("deleted_at", "ingested_at"),
    ),
    (
        "ix_sessions_deleted_started",
        "sessions",
        ("deleted_at", "started_at"),
    ),
    (
        "ix_conversations_ended_at",
        "conversations",
        ("ended_at",),
    ),
    (
        "ix_catalogs_parent_live_name",
        "catalogs",
        ("parent_id", "deleted_at", "name"),
    ),
    (
        "ix_catalogs_live_name",
        "catalogs",
        ("deleted_at", "name"),
    ),
    (
        "ix_views_name",
        "views",
        ("name",),
    ),
    (
        "ix_tags_facet_alias_doc_count",
        "tags",
        ("facet", "alias_of", "doc_count", "name"),
    ),
    (
        "ix_tags_alias_doc_count",
        "tags",
        ("alias_of", "doc_count"),
    ),
    (
        "ix_entry_relations_vetted",
        "entry_relations",
        ("vetted",),
    ),
    (
        "ix_journal_kind_created",
        "journal",
        ("source_kind", "created_at"),
    ),
    (
        "ix_journal_active_created",
        "journal",
        ("superseded_by_id", "created_at"),
    ),
    (
        "ix_journal_kind_active_created",
        "journal",
        ("source_kind", "superseded_by_id", "created_at"),
    ),
    (
        "ix_journal_invalidated_created",
        "journal",
        ("invalidated_at", "created_at"),
    ),
    (
        "ix_task_outcomes_lookup_completed",
        "task_outcomes",
        ("task_kind", "object_kind", "object_id", "completed_at"),
    ),
    (
        "ix_task_outcomes_kind_object_completed",
        "task_outcomes",
        ("task_kind", "object_kind", "completed_at"),
    ),
    (
        "ix_tasks_claim",
        "tasks",
        ("status", "priority", "scheduled_at"),
    ),
    (
        "ix_tasks_status_lease",
        "tasks",
        ("status", "lease_expires_at"),
    ),
    (
        "ix_tasks_kind_status_finished",
        "tasks",
        ("kind", "status", "finished_at"),
    ),
    (
        "ix_tasks_status_finished",
        "tasks",
        ("status", "finished_at"),
    ),
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


def _quote_ident(name: str) -> str:
    if '"' in name:
        raise ValueError(f"invalid identifier: {name!r}")
    return f'"{name}"'


def _ensure_query_performance_indexes(bind) -> None:
    """Add query-shape indexes that landed after the baseline schema.

    Fresh databases get these through SQLAlchemy model metadata. Existing
    databases need explicit CREATE INDEX calls because create_all() does not
    backfill indexes on already-created tables.
    """
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for index_name, table_name, columns in QUERY_PERFORMANCE_INDEXES:
        if table_name not in existing_tables:
            continue
        column_sql = ", ".join(_quote_ident(c) for c in columns)
        bind.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {_quote_ident(index_name)} "
            f"ON {_quote_ident(table_name)} ({column_sql})"
        ))


def _ensure_postgres_metadata_fts_indexes(bind) -> None:
    """Add Postgres expression GIN indexes for metadata text search.

    SQLite uses the FTS5 virtual table below. Postgres searches the joined
    `file_entries` and `files` metadata surfaces directly with to_tsvector,
    so each side gets its own expression index.
    """
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if "file_entries" in existing_tables:
        bind.execute(sa.text("""
            CREATE INDEX IF NOT EXISTS ix_file_entries_metadata_fts_pg
            ON file_entries USING gin (
                to_tsvector(
                    'simple',
                    COALESCE(display_name, '') || ' ' || COALESCE(extra, '')
                )
            )
            WHERE deleted_at IS NULL
        """))
    if "files" in existing_tables:
        bind.execute(sa.text("""
            CREATE INDEX IF NOT EXISTS ix_files_metadata_fts_pg
            ON files USING gin (
                to_tsvector(
                    'simple',
                    COALESCE(summary, '') || ' ' ||
                    COALESCE(description::text, '') || ' ' ||
                    COALESCE(extra, '') || ' ' ||
                    COALESCE(original_ext, '')
                )
            )
            WHERE deleted_at IS NULL
        """))


def _drop_postgres_metadata_fts_indexes(bind) -> None:
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_file_entries_metadata_fts_pg"))
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_files_metadata_fts_pg"))


def _ensure_journal_invalidation(bind) -> None:
    _apply_additive_columns(bind)
    _ensure_query_performance_indexes(bind)


def _reconcile_dead_ingest_files(bind) -> None:
    """Repair files left pending/processing after terminal ingest task death."""
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if not {"files", "tasks"}.issubset(existing_tables):
        return

    now = datetime.now(timezone.utc)
    if bind.dialect.name == "postgresql":
        file_id_expr = "payload ->> 'file_id'"
    else:
        file_id_expr = "json_extract(payload, '$.file_id')"

    bind.execute(
        sa.text(f"""
            UPDATE files
            SET ingest_status = 'failed',
                updated_at = :now
            WHERE deleted_at IS NULL
              AND ingest_status IN ('pending', 'processing')
              AND EXISTS (
                  SELECT 1
                  FROM tasks dead
                  WHERE dead.kind = 'ingest_file'
                    AND dead.status = 'dead'
                    AND {file_id_expr.replace('payload', 'dead.payload')} = files.id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM tasks active
                  WHERE active.kind = 'ingest_file'
                    AND active.status IN ('pending', 'running')
                    AND {file_id_expr.replace('payload', 'active.payload')} = files.id
              )
        """),
        {"now": now},
    )


def _sqlite_supports_metadata_fts(bind) -> bool:
    if bind.dialect.name != "sqlite":
        return False
    try:
        bind.execute(sa.text(
            "CREATE VIRTUAL TABLE temp._marginalia_fts5_probe "
            "USING fts5(x, tokenize='trigram')"
        ))
        bind.execute(sa.text("DROP TABLE temp._marginalia_fts5_probe"))
    except Exception:
        return False
    return True


def _ensure_entry_metadata_fts(bind) -> None:
    """Create a SQLite FTS5 trigram index over already-stored metadata.

    This intentionally indexes only existing DB metadata surfaces, not raw
    file contents: file_entries.display_name, file_entries.extra, files.summary,
    files.description, files.extra, and files.original_ext. Triggers keep the
    virtual table in sync after the initial backfill.
    """
    if not _sqlite_supports_metadata_fts(bind):
        return
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    if not {"files", "file_entries"}.issubset(existing_tables):
        return
    if (
        ENTRY_METADATA_FTS_TABLE in existing_tables
        and "file_description" not in _entry_metadata_fts_columns(bind)
    ):
        _drop_entry_metadata_fts(bind)

    bind.execute(sa.text(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {ENTRY_METADATA_FTS_TABLE}
        USING fts5(
            entry_id UNINDEXED,
            display_name,
            file_summary,
            file_description,
            file_extra,
            file_original_ext,
            entry_extra,
            tokenize='trigram'
        )
    """))
    bind.execute(sa.text(f"""
        CREATE TRIGGER IF NOT EXISTS entry_metadata_fts_file_entries_ai
        AFTER INSERT ON file_entries
        BEGIN
            INSERT INTO {ENTRY_METADATA_FTS_TABLE}
                (rowid, entry_id, display_name, file_summary, file_description,
                 file_extra, file_original_ext, entry_extra)
            SELECT
                new.rowid,
                new.id,
                COALESCE(new.display_name, ''),
                COALESCE(files.summary, ''),
                COALESCE(files.description, ''),
                COALESCE(files.extra, ''),
                COALESCE(files.original_ext, ''),
                COALESCE(new.extra, '')
            FROM files
            WHERE files.id = new.file_id;
        END
    """))
    bind.execute(sa.text(f"""
        CREATE TRIGGER IF NOT EXISTS entry_metadata_fts_file_entries_au
        AFTER UPDATE OF id, file_id, display_name, extra ON file_entries
        BEGIN
            DELETE FROM {ENTRY_METADATA_FTS_TABLE}
            WHERE rowid = old.rowid;
            INSERT INTO {ENTRY_METADATA_FTS_TABLE}
                (rowid, entry_id, display_name, file_summary, file_description,
                 file_extra, file_original_ext, entry_extra)
            SELECT
                new.rowid,
                new.id,
                COALESCE(new.display_name, ''),
                COALESCE(files.summary, ''),
                COALESCE(files.description, ''),
                COALESCE(files.extra, ''),
                COALESCE(files.original_ext, ''),
                COALESCE(new.extra, '')
            FROM files
            WHERE files.id = new.file_id;
        END
    """))
    bind.execute(sa.text(f"""
        CREATE TRIGGER IF NOT EXISTS entry_metadata_fts_file_entries_ad
        AFTER DELETE ON file_entries
        BEGIN
            DELETE FROM {ENTRY_METADATA_FTS_TABLE}
            WHERE rowid = old.rowid;
        END
    """))
    bind.execute(sa.text(f"""
        CREATE TRIGGER IF NOT EXISTS entry_metadata_fts_files_au
        AFTER UPDATE OF summary, description, extra, original_ext ON files
        BEGIN
            DELETE FROM {ENTRY_METADATA_FTS_TABLE}
            WHERE rowid IN (
                SELECT rowid FROM file_entries WHERE file_id = old.id
            );
            INSERT INTO {ENTRY_METADATA_FTS_TABLE}
                (rowid, entry_id, display_name, file_summary, file_description,
                 file_extra, file_original_ext, entry_extra)
            SELECT
                file_entries.rowid,
                file_entries.id,
                COALESCE(file_entries.display_name, ''),
                COALESCE(new.summary, ''),
                COALESCE(new.description, ''),
                COALESCE(new.extra, ''),
                COALESCE(new.original_ext, ''),
                COALESCE(file_entries.extra, '')
            FROM file_entries
            WHERE file_entries.file_id = new.id;
        END
    """))
    bind.execute(sa.text(f"""
        CREATE TRIGGER IF NOT EXISTS entry_metadata_fts_files_ad
        AFTER DELETE ON files
        BEGIN
            DELETE FROM {ENTRY_METADATA_FTS_TABLE}
            WHERE rowid IN (
                SELECT rowid FROM file_entries WHERE file_id = old.id
            );
        END
    """))
    bind.execute(sa.text(f"""
        INSERT INTO {ENTRY_METADATA_FTS_TABLE}
            (rowid, entry_id, display_name, file_summary, file_description,
             file_extra, file_original_ext, entry_extra)
        SELECT
            file_entries.rowid,
            file_entries.id,
            COALESCE(file_entries.display_name, ''),
            COALESCE(files.summary, ''),
            COALESCE(files.description, ''),
            COALESCE(files.extra, ''),
            COALESCE(files.original_ext, ''),
            COALESCE(file_entries.extra, '')
        FROM file_entries
        JOIN files ON files.id = file_entries.file_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM {ENTRY_METADATA_FTS_TABLE} existing
            WHERE existing.rowid = file_entries.rowid
        )
    """))


def _entry_metadata_fts_columns(bind) -> set[str]:
    if bind.dialect.name != "sqlite":
        return set()
    try:
        rows = bind.execute(sa.text(
            f"PRAGMA table_info({_quote_ident(ENTRY_METADATA_FTS_TABLE)})"
        )).fetchall()
    except Exception:
        return set()
    return {str(row[1]) for row in rows}


def _ensure_entry_metadata_fts_description(bind) -> None:
    """Rebuild older SQLite FTS tables so file.description is searchable."""
    if bind.dialect.name != "sqlite":
        return
    inspector = sa.inspect(bind)
    if ENTRY_METADATA_FTS_TABLE in inspector.get_table_names():
        columns = _entry_metadata_fts_columns(bind)
        if "file_description" not in columns:
            _drop_entry_metadata_fts(bind)
    _ensure_entry_metadata_fts(bind)


def _drop_entry_metadata_fts(bind) -> None:
    if bind.dialect.name != "sqlite":
        return
    for trigger_name in ENTRY_METADATA_FTS_TRIGGERS:
        bind.execute(sa.text(f"DROP TRIGGER IF EXISTS {_quote_ident(trigger_name)}"))
    bind.execute(sa.text(
        f"DROP TABLE IF EXISTS {_quote_ident(ENTRY_METADATA_FTS_TABLE)}"
    ))


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


def _ensure_conversations_session_turn_unique(bind) -> None:
    """Replace the legacy non-unique `ix_conversations_session_turn` index
    with a unique constraint on (session_id, turn_index).

    Why: `run_turn` computes the next turn via
    `latest_turn_index(session) + 1` then INSERTs. Two concurrent requests
    for the same session race on the read-modify-write and write two rows
    with identical (session_id, turn_index). The route layer now also
    serialises with a per-session asyncio.Lock, but that only covers
    one Python process — this constraint is the cross-process backstop.

    Idempotent:
      - if the unique constraint/index already exists → no-op.
      - if the legacy non-unique index is present → drop it, then create
        the unique one.
      - if existing rows already violate the invariant → raise. We do NOT
        silently merge / renumber: that would falsify history. Operator
        decides what to do (almost certainly: delete one of the dupes).
    """
    inspector = sa.inspect(bind)
    if "conversations" not in inspector.get_table_names():
        return

    indexes = inspector.get_indexes("conversations")
    has_unique = any(
        idx["name"] == "uq_conversations_session_turn"
        and idx.get("unique", False)
        for idx in indexes
    )
    if has_unique:
        return

    dup_rows = bind.execute(sa.text(
        "SELECT session_id, turn_index, COUNT(*) AS n "
        "FROM conversations "
        "GROUP BY session_id, turn_index "
        "HAVING COUNT(*) > 1 "
        "LIMIT 5"
    )).fetchall()
    if dup_rows:
        sample = ", ".join(
            f"(session={r[0]!r}, turn={r[1]}, count={r[2]})" for r in dup_rows
        )
        raise RuntimeError(
            "Cannot enforce UNIQUE(session_id, turn_index) on conversations: "
            f"existing duplicates found — {sample}. Resolve manually "
            "(usually: DELETE FROM conversations WHERE id = '<id-of-dup>') "
            "and restart."
        )

    # Drop the legacy non-unique covering index if present; the unique
    # constraint we add below covers the same query plan plus the
    # invariant. Both SQLite and Postgres accept IF EXISTS.
    bind.execute(sa.text("DROP INDEX IF EXISTS ix_conversations_session_turn"))
    bind.execute(sa.text(
        "CREATE UNIQUE INDEX uq_conversations_session_turn "
        "ON conversations (session_id, turn_index)"
    ))


def _ensure_tasks_active_dedup_unique(bind) -> None:
    """Enforce at most one pending/running task per dedup_key.

    `enqueue()` performs a best-effort read before insert, but concurrent
    workers need the database to be the source of truth. Done/dead rows are
    historical facts and may keep the same dedup_key.
    """
    inspector = sa.inspect(bind)
    if "tasks" not in inspector.get_table_names():
        return

    indexes = inspector.get_indexes("tasks")
    has_unique = any(
        idx["name"] == "uq_tasks_active_dedup_key"
        and idx.get("unique", False)
        for idx in indexes
    )
    if has_unique:
        return

    dup_rows = bind.execute(sa.text(
        "SELECT dedup_key, COUNT(*) AS n "
        "FROM tasks "
        "WHERE dedup_key IS NOT NULL AND status IN ('pending', 'running') "
        "GROUP BY dedup_key "
        "HAVING COUNT(*) > 1 "
        "LIMIT 5"
    )).fetchall()
    if dup_rows:
        sample = ", ".join(
            f"(dedup_key={r[0]!r}, count={r[1]})" for r in dup_rows
        )
        raise RuntimeError(
            "Cannot enforce unique active task dedup_key: existing active "
            f"duplicates found - {sample}. Resolve or finish duplicates and restart."
        )

    bind.execute(sa.text(
        "CREATE UNIQUE INDEX uq_tasks_active_dedup_key "
        "ON tasks (dedup_key) "
        "WHERE dedup_key IS NOT NULL AND status IN ('pending', 'running')"
    ))


def bootstrap_baseline_sync(bind) -> None:
    """v1 baseline — `Base.metadata.create_all` plus the `_inbox` seed.

    Mirrors what alembic 0001_initial owns: a fresh database after this
    runs is structurally identical to one created by `alembic upgrade
    0001_initial` against an empty schema. None of the post-v1 shims
    (`_apply_additive_columns`, `_relax_*`, …) run here — those are
    revisions 0002+.
    """
    Base.metadata.create_all(bind=bind)
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


# Ordered list of post-baseline shims. Each entry: (alembic-revision-id,
# helper). Adding a new shim means: append to this list, drop a
# corresponding `000X_*.py` revision in alembic/versions/ that calls the
# same helper. App-startup bootstrap runs all of them; alembic runs them
# one at a time as separate revisions.
POST_BASELINE_SHIMS: tuple[tuple[str, Callable[[Any], None]], ...] = (
    ("0002_additive_columns", _apply_additive_columns),
    ("0003_file_entries_folder_id_nullable", _relax_file_entries_folder_id_nullable),
    ("0004_repair_dangling_file_entries_fks", _repair_dangling_file_entries_fks),
    ("0005_sessions_end_reason_check", _relax_sessions_end_reason_check),
    ("0006_conversations_session_turn_unique", _ensure_conversations_session_turn_unique),
    ("0007_tasks_active_dedup_unique", _ensure_tasks_active_dedup_unique),
    ("0008_query_performance_indexes", _ensure_query_performance_indexes),
    ("0009_entry_metadata_fts", _ensure_entry_metadata_fts),
    ("0010_entry_metadata_fts_description", _ensure_entry_metadata_fts_description),
    ("0011_postgres_metadata_fts", _ensure_postgres_metadata_fts_indexes),
    ("0012_journal_invalidation", _ensure_journal_invalidation),
    ("0013_reconcile_dead_ingest_files", _reconcile_dead_ingest_files),
)

ALEMBIC_HEAD_REVISION = POST_BASELINE_SHIMS[-1][0]


def _stamp_alembic_version(bind, revision: str) -> None:
    """Make `alembic_version` reflect that `revision` is applied.

    Bootstraps a fresh DB to head (no migration ran, but the schema is
    equivalent), or upgrades the stamp on an existing DB once
    bootstrap-time shims have caught it up. Idempotent.
    """
    bind.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS alembic_version ("
        "version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
    ))
    bind.execute(sa.text("DELETE FROM alembic_version"))
    bind.execute(
        sa.text("INSERT INTO alembic_version (version_num) VALUES (:r)"),
        {"r": revision},
    )


def bootstrap_schema_sync(bind) -> None:
    """Synchronous full bootstrap — baseline + every post-v1 shim + stamp.

    Used by `bootstrap_schema()` below via `run_sync`. After this, the
    DB matches the current model and `alembic_version` says HEAD, so an
    operator running `alembic upgrade head` against the same DB sees a
    no-op.
    """
    bootstrap_baseline_sync(bind)
    for _rev, helper in POST_BASELINE_SHIMS:
        helper(bind)
    _stamp_alembic_version(bind, ALEMBIC_HEAD_REVISION)


async def bootstrap_schema() -> None:
    """Run schema creation + inbox seed against the configured async engine."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(bootstrap_schema_sync)
