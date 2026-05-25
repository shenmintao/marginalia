"""tags repository — pure SA queries against the Tag and TagAlias tables.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryTag, Tag, TagAlias


async def find_by_name(
    db: AsyncSession, name: str, *, facet: str | None = None,
) -> list[Tag]:
    """All Tag rows whose name matches (case-sensitive). Used by resolve_tag's
    direct match step. The facet filter, when given, is applied server-side."""
    stmt = select(Tag).where(Tag.name == name)
    if facet is not None:
        stmt = stmt.where(Tag.facet == facet)
    return list((await db.execute(stmt)).scalars().all())


async def list_aliases_from(
    db: AsyncSession, name: str,
) -> list[TagAlias]:
    """tag_aliases rows whose from_name matches. Used by resolve_tag's
    fallback step."""
    return list(
        (
            await db.execute(
                select(TagAlias).where(TagAlias.from_name == name)
            )
        ).scalars().all()
    )


async def top_per_facet(
    db: AsyncSession, facet: str, *, limit: int,
) -> list[tuple[str, str, int | None]]:
    """Top-N canonical tags (alias_of IS NULL) in one facet, ordered by
    doc_count desc then name. Returns `(id, name, doc_count)` tuples; the
    snapshot doesn't need the full ORM row."""
    rows = (
        await db.execute(
            select(Tag.id, Tag.name, Tag.doc_count)
            .where(Tag.facet == facet, Tag.alias_of.is_(None))
            .order_by(Tag.doc_count.desc(), Tag.name)
            .limit(limit)
        )
    ).all()
    return [(tid, n, dc) for tid, n, dc in rows]


async def list_facet_tag_summaries(
    db: AsyncSession, facet: str,
) -> list[tuple[str, str, str | None, int | None]]:
    """All `(id, name, alias_of, doc_count)` rows in a facet, ordered by
    doc_count desc then name. Used by normalize_tags."""
    rows = (
        await db.execute(
            select(Tag.id, Tag.name, Tag.alias_of, Tag.doc_count)
            .where(Tag.facet == facet)
            .order_by(Tag.doc_count.desc(), Tag.name)
        )
    ).all()
    return [(tid, n, a, dc) for tid, n, a, dc in rows]


async def all_ids(db: AsyncSession) -> list[str]:
    """Every tag id; used by normalize_tags' doc_count recompute."""
    return list(
        (await db.execute(select(Tag.id))).scalars().all()
    )


async def entry_tag_counts_by_tag(
    db: AsyncSession,
) -> dict[str, int]:
    """Map `tag_id -> count(entry_tags)` for every tag that has at least one
    entry_tags row. Used by normalize_tags' doc_count recompute."""
    rows = (
        await db.execute(
            select(EntryTag.tag_id, func.count())
            .group_by(EntryTag.tag_id)
        )
    ).all()
    return {tag_id: int(c) for tag_id, c in rows}


async def set_doc_count(
    db: AsyncSession, *, tag_id: str, doc_count: int,
) -> None:
    """Used by normalize_tags after a merge run."""
    await db.execute(
        update(Tag).where(Tag.id == tag_id).values(doc_count=doc_count)
    )


async def find_canonical_by_name_facet(
    db: AsyncSession, *, name: str, facet: str,
) -> Tag | None:
    """Used by ingest_file's tag resolution step. Returns the row whether
    canonical or alias; the caller follows alias_of."""
    return (
        await db.execute(
            select(Tag).where(Tag.name == name, Tag.facet == facet)
        )
    ).scalar_one_or_none()


async def list_canonical_summaries(
    db: AsyncSession, *, limit: int,
) -> list[tuple[str, str | None, int | None]]:
    """Top-N canonical tags as `(name, facet, doc_count)` tuples,
    ordered by doc_count desc. Used by ingest's pipeline context."""
    rows = (
        await db.execute(
            select(Tag.name, Tag.facet, Tag.doc_count)
            .where(Tag.alias_of.is_(None))
            .order_by(Tag.doc_count.desc())
            .limit(limit)
        )
    ).all()
    return [(n, f, dc) for n, f, dc in rows]


async def name_by_ids(
    db: AsyncSession, ids: list[str],
) -> dict[str, str]:
    """Map `tag_id -> name` for the given ids. Used by the agent runtime
    so tool_call display can render `tags 'concept-id'` as `tags 'foo'`."""
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Tag.id, Tag.name).where(Tag.id.in_(ids))
        )
    ).all()
    return {tid: n for tid, n in rows}


async def list_canonical_id_name(
    db: AsyncSession,
) -> list[tuple[str, str]]:
    """`(id, name)` for every canonical tag (alias_of IS NULL). Used by
    propose_views to label clusters."""
    rows = (
        await db.execute(
            select(Tag.id, Tag.name).where(Tag.alias_of.is_(None))
        )
    ).all()
    return [(tid, n) for tid, n in rows]


async def list_canonical_per_facet(
    db: AsyncSession, *, facet: str, limit: int,
) -> list[tuple[str, str, int | None]]:
    """`(id, name, doc_count)` for the top-N canonical tags in `facet`.
    Used by enrich_tags' vocabulary feed."""
    rows = (
        await db.execute(
            select(Tag.id, Tag.name, Tag.doc_count)
            .where(Tag.facet == facet, Tag.alias_of.is_(None))
            .order_by(Tag.doc_count.desc(), Tag.name)
            .limit(limit)
        )
    ).all()
    return [(tid, n, dc) for tid, n, dc in rows]


async def delete_entry_tag_dups_for_merge(
    db: AsyncSession, *, merged_tag_id: str, canonical_tag_id: str,
) -> None:
    """Step 1 of an entry_tags rewrite. Delete rows whose tag_id is the
    merged tag and whose entry already has the canonical tag (PK conflict
    avoidance). Used by normalize_tags."""
    await db.execute(
        delete(EntryTag).where(
            EntryTag.tag_id == merged_tag_id,
            EntryTag.entry_id.in_(
                select(EntryTag.entry_id).where(
                    EntryTag.tag_id == canonical_tag_id
                )
            ),
        )
    )


async def repoint_entry_tags(
    db: AsyncSession, *, from_tag_id: str, to_tag_id: str,
) -> int:
    """Step 2 of an entry_tags rewrite. Update remaining rows so they point
    at the canonical tag. Returns row count."""
    result = await db.execute(
        update(EntryTag)
        .where(EntryTag.tag_id == from_tag_id)
        .values(tag_id=to_tag_id)
    )
    return int(result.rowcount or 0)
