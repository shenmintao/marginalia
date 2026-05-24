"""files repository — pure SA queries against the File table.

Caller owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File


async def get_by_sha256(db: AsyncSession, sha256: str) -> File | None:
    """Live or soft-deleted file row matching the content hash. Used by
    upload to detect dedup hits before a tentative storage put is finalised."""
    return (
        await db.execute(select(File).where(File.sha256 == sha256))
    ).scalar_one_or_none()
