from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect

from marginalia.config import Settings
from marginalia.db.bootstrap import (
    QUERY_PERFORMANCE_INDEXES,
    bootstrap_schema_sync,
)
from marginalia.db.engine import _build_engine
from marginalia.db.models import Base


def test_query_performance_indexes_are_modelled_and_bootstrapped(tmp_path) -> None:
    metadata_indexes = {
        index.name
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }
    for index_name, _table_name, _columns in QUERY_PERFORMANCE_INDEXES:
        assert index_name in metadata_indexes

    engine = create_engine(f"sqlite:///{tmp_path / 'marginalia.db'}")
    try:
        with engine.begin() as conn:
            bootstrap_schema_sync(conn)
            inspector = inspect(conn)
            indexes_by_table = {
                table_name: {idx["name"] for idx in inspector.get_indexes(table_name)}
                for _index_name, table_name, _columns in QUERY_PERFORMANCE_INDEXES
            }
            for index_name, table_name, _columns in QUERY_PERFORMANCE_INDEXES:
                assert index_name in indexes_by_table[table_name]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_engine_sets_performance_pragmas(tmp_path) -> None:
    settings = Settings(marginalia_home=str(tmp_path))
    engine = _build_engine(settings)
    try:
        async with engine.connect() as conn:
            journal_mode = (
                await conn.exec_driver_sql("PRAGMA journal_mode")
            ).scalar_one()
            synchronous = (
                await conn.exec_driver_sql("PRAGMA synchronous")
            ).scalar_one()
            busy_timeout = (
                await conn.exec_driver_sql("PRAGMA busy_timeout")
            ).scalar_one()
            cache_size = (
                await conn.exec_driver_sql("PRAGMA cache_size")
            ).scalar_one()
            temp_store = (
                await conn.exec_driver_sql("PRAGMA temp_store")
            ).scalar_one()

        assert str(journal_mode).lower() == "wal"
        assert int(synchronous) == 1
        assert int(busy_timeout) == 30000
        assert int(cache_size) == -65536
        assert int(temp_store) == 2
    finally:
        await engine.dispose()
