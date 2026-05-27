"""entry_relations repository — pure SA queries against the EntryRelation table.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryRelation, File, FileEntry


async def list_top_for_entry(
    db: AsyncSession, entry_id: str, *, limit: int,
) -> list[EntryRelation]:
    """Top-`limit` relations touching `entry_id` (either side), ordered by
    observation_count desc, then last_observed_at desc."""
    rows = (
        await db.execute(
            select(EntryRelation)
            .where(or_(
                EntryRelation.entry_a_id == entry_id,
                EntryRelation.entry_b_id == entry_id,
            ))
            .order_by(
                EntryRelation.observation_count.desc(),
                EntryRelation.last_observed_at.desc(),
            )
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


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


async def find_pair(
    db: AsyncSession, *, entry_a_id: str, entry_b_id: str,
) -> EntryRelation | None:
    """Find the existing relation row for a `(a, b)` pair (caller must
    have sorted them stably). Used by upsert_relation_pair."""
    return (
        await db.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == entry_a_id,
                EntryRelation.entry_b_id == entry_b_id,
            )
        )
    ).scalar_one_or_none()


async def bump_observation(
    db: AsyncSession,
    *,
    relation_id: str,
    new_count: int,
    last_observed_at: datetime,
    source_kind: str,
    note: str,
) -> None:
    """Step 2 of upsert_relation_pair when a row already exists. Bumps the
    observation_count, refreshes last_observed_at, and overwrites
    source_kind/note to reflect the most recent miner's attribution.

    Provenance history (which miner observed this pair when, with what
    payload) lives in audit_events.relation_mined — the entry_relations
    row only needs the latest writer's metadata.
    """
    await db.execute(
        update(EntryRelation)
        .where(EntryRelation.id == relation_id)
        .values(
            observation_count=new_count,
            last_observed_at=last_observed_at,
            source_kind=source_kind,
            note=note,
        )
    )


async def update_vetted(
    db: AsyncSession,
    *,
    relation_id: str,
    vetted: bool,
    vetted_reason: str,
    vetted_at: datetime,
    vetted_observation_count: int,
) -> None:
    """Used by vet_relations to mark an LLM verdict on a relation row."""
    await db.execute(
        update(EntryRelation)
        .where(EntryRelation.id == relation_id)
        .values(
            vetted=vetted,
            vetted_reason=vetted_reason,
            vetted_at=vetted_at,
            vetted_observation_count=vetted_observation_count,
        )
    )


async def list_vet_candidates(
    db: AsyncSession, *, min_obs: int,
) -> list[dict[str, Any]]:
    """Pull every relation joined to both endpoints' FileEntry+File rows so
    vet_relations can decide which need (re)vetting. Filters: both endpoints
    live, both files have a non-NULL summary, observation_count >= min_obs.
    Ordered by observation_count desc.

    Returns dicts (not raw tuples) because vet_relations consumes lots of
    columns and a row tuple would be unreadable."""
    from sqlalchemy.orm import aliased
    FA = aliased(FileEntry, name="fa")
    FB = aliased(FileEntry, name="fb")
    FilA = aliased(File, name="filA")
    FilB = aliased(File, name="filB")
    rows = (
        await db.execute(
            select(
                EntryRelation.id,
                EntryRelation.entry_a_id,
                EntryRelation.entry_b_id,
                EntryRelation.observation_count,
                EntryRelation.vetted,
                EntryRelation.vetted_at,
                EntryRelation.vetted_observation_count,
                EntryRelation.note,
                EntryRelation.source_kind,
                FA.display_name.label("a_name"),
                FB.display_name.label("b_name"),
                FilA.summary.label("a_summary"),
                FilB.summary.label("b_summary"),
                FilA.kind.label("a_kind"),
                FilB.kind.label("b_kind"),
            )
            .select_from(EntryRelation)
            .join(FA, FA.id == EntryRelation.entry_a_id)
            .join(FB, FB.id == EntryRelation.entry_b_id)
            .join(FilA, FilA.id == FA.file_id)
            .join(FilB, FilB.id == FB.file_id)
            .where(
                FA.deleted_at.is_(None),
                FB.deleted_at.is_(None),
                EntryRelation.observation_count >= min_obs,
                FilA.summary.is_not(None),
                FilB.summary.is_not(None),
            )
            .order_by(EntryRelation.observation_count.desc())
        )
    ).all()
    return [
        {
            "id": r[0],
            "entry_a_id": r[1],
            "entry_b_id": r[2],
            "observation_count": r[3],
            "vetted": r[4],
            "vetted_at": r[5],
            "vetted_observation_count": r[6],
            "note": r[7],
            "source_kind": r[8],
            "a_name": r[9],
            "b_name": r[10],
            "a_summary": r[11],
            "b_summary": r[12],
            "a_kind": r[13],
            "b_kind": r[14],
        }
        for r in rows
    ]


async def list_pair_keys(db: AsyncSession) -> list[tuple[str, str]]:
    """`(entry_a_id, entry_b_id)` for every relation row. Used by
    mine_corpus_evidence to skip pairs already linked."""
    rows = (
        await db.execute(
            select(EntryRelation.entry_a_id, EntryRelation.entry_b_id)
        )
    ).all()
    return [(a, b) for a, b in rows]


async def list_vetted_pair_keys(db: AsyncSession) -> list[tuple[str, str]]:
    """`(entry_a_id, entry_b_id)` for every vetted=True relation. Used by
    propose_views' relation-graph cluster builder."""
    rows = (
        await db.execute(
            select(EntryRelation.entry_a_id, EntryRelation.entry_b_id)
            .where(EntryRelation.vetted.is_(True))
        )
    ).all()
    return [(a, b) for a, b in rows]
