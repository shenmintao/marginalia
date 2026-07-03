from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy.ext.asyncio import AsyncEngine

from marginalia.config import get_settings
from marginalia.db.engine import get_engine
from marginalia.db.models import Base  # registers all tables

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _ensure_wide_version_table(connection) -> None:
    """Several revision ids in this chain exceed alembic's default
    `version_num VARCHAR(32)`, and Postgres enforces the length — the
    upgrade would abort mid-chain writing e.g.
    `0004_repair_dangling_file_entries_fks`. Pre-create the table wide
    (alembic reuses an existing one) or widen it in place. SQLite never
    enforces VARCHAR lengths, so no ALTER is needed there."""
    if not sa.inspect(connection).has_table("alembic_version"):
        connection.execute(sa.text(
            "CREATE TABLE alembic_version ("
            "version_num VARCHAR(255) NOT NULL PRIMARY KEY)"
        ))
        return
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(sa.text(
            "ALTER TABLE alembic_version "
            "ALTER COLUMN version_num TYPE VARCHAR(255)"
        ))
    elif dialect in ("mysql", "mariadb"):
        connection.execute(sa.text(
            "ALTER TABLE alembic_version "
            "MODIFY version_num VARCHAR(255) NOT NULL"
        ))


def do_run_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine: AsyncEngine = get_engine()
    # Separate committed transaction: the widen must be visible before
    # alembic opens its own migration transaction on a fresh connection.
    async with engine.begin() as conn:
        await conn.run_sync(_ensure_wide_version_table)
    async with engine.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
