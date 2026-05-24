"""entry_tags repository — pure SA queries against the EntryTag table.

Caller owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryTag


async def list_tag_ids_for_entry(
    db: AsyncSession, entry_id: str,
) -> list[str]:
    """All tag_ids attached to `entry_id`. Used by upload's dedup path to
    copy tags from a seed entry onto the new entry."""
    rows = (
        await db.execute(
            select(EntryTag.tag_id).where(EntryTag.entry_id == entry_id)
        )
    ).scalars().all()
    return list(rows)
