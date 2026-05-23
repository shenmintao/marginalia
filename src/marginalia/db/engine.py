from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from marginalia.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[Any] | None = None


def _build_engine(settings: Settings) -> AsyncEngine:
    url = settings.database_url
    if settings.db_backend == "sqlite":
        engine = create_async_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

        return engine

    return create_async_engine(url, future=True, pool_pre_ping=True)


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = _build_engine(get_settings())
    return _engine


def get_session_factory() -> async_sessionmaker[Any]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
