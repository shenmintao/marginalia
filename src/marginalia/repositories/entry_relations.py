"""entry_relations repository — pure SA queries against the EntryRelation table.

Caller owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryRelation, FileEntry


async def list_edges_with_live_a(
    db: AsyncSession, *, vetted_only: bool,
) -> list[tuple[str, str, int]]:
    """Return `(entry_a_id, entry_b_id, observation_count)` rows where
    side A is live. The B side is filtered separately by the caller.

    The two-step shape (vs a self-join with two filters) is intentional: the
    SQLite planner doesn't always optimise the double-FK case."""
    stmt = (
        select(
            EntryRelation.entry_a_id,
            EntryRelation.entry_b_id,
            EntryRelation.observation_count,
        )
        .join(FileEntry, FileEntry.id == EntryRelation.entry_a_id)
        .where(FileEntry.deleted_at.is_(None))
    )
    if vetted_only:
        stmt = stmt.where(EntryRelation.vetted.is_(True))
    rows = (await db.execute(stmt)).all()
    return [(a, b, w) for a, b, w in rows]
