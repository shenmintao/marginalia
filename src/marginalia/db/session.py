from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.engine import get_session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Standalone async session as a context manager."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a fresh session per request."""
    factory = get_session_factory()
    async with factory() as session:
        yield session
