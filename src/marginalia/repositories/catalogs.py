"""catalogs repository — pure SA queries against the Catalog table.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Catalog, FileEntry


async def expand_subtree(db: AsyncSession, root_id: str) -> list[str]:
    """BFS-collect ids of `root_id` plus every live catalog beneath it.
    Used by anything filtering entries by catalog_subtree (search_metadata,
    materialize_view, restructure_catalogs)."""
    seen: set[str] = {root_id}
    frontier: list[str] = [root_id]
    while frontier:
        children = (
            await db.execute(
                select(Catalog.id).where(
                    Catalog.parent_id.in_(frontier),
                    Catalog.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        new = [c for c in children if c not in seen]
        if not new:
            break
        seen.update(new)
        frontier = new
    return list(seen)


async def get_live(db: AsyncSession, catalog_id: str) -> Catalog | None:
    return (
        await db.execute(
            select(Catalog).where(
                Catalog.id == catalog_id,
                Catalog.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def name_by_ids(
    db: AsyncSession, ids: list[str],
) -> dict[str, str]:
    """Map `catalog_id -> name`. Used by the agent runtime so tool_call
    display can render `read_catalog "Algorithms"` instead of a uuid.
    Includes soft-deleted catalogs so replay of older transcripts still
    resolves."""
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Catalog.id, Catalog.name).where(Catalog.id.in_(ids))
        )
    ).all()
    return {cid: n for cid, n in rows}


async def list_live_children(
    db: AsyncSession, parent_id: str | None,
    *, limit: int | None = None, offset: int = 0,
) -> list[Catalog]:
    """Live children of `parent_id` (None = root). When `limit` is None,
    returns the entire list (legacy behavior). Agent tools pass both
    `limit` and `offset` to honor the pagination contract."""
    stmt = (
        select(Catalog)
        .where(
            Catalog.parent_id.is_(None) if parent_id is None else Catalog.parent_id == parent_id,
            Catalog.deleted_at.is_(None),
        )
        .order_by(Catalog.name)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def count_live_children(
    db: AsyncSession, parent_id: str | None,
) -> int:
    """Total live children of `parent_id`. Pair with paginated list_live_children."""
    stmt = select(func.count()).select_from(Catalog).where(
        Catalog.parent_id.is_(None) if parent_id is None else Catalog.parent_id == parent_id,
        Catalog.deleted_at.is_(None),
    )
    return int((await db.execute(stmt)).scalar_one())


async def list_live_top_level(
    db: AsyncSession, *, limit: int,
) -> list[Catalog]:
    """First N live root catalogs, ordered by name. Used by the agent's
    stable-context snapshot."""
    rows = (
        await db.execute(
            select(Catalog)
            .where(Catalog.parent_id.is_(None), Catalog.deleted_at.is_(None))
            .order_by(Catalog.name)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def list_all_live(db: AsyncSession) -> list[Catalog]:
    """All live catalogs. Used by list_catalogs and the catalog
    restructuring miners."""
    rows = (
        await db.execute(
            select(Catalog)
            .where(Catalog.deleted_at.is_(None))
            .order_by(Catalog.name)
        )
    ).scalars().all()
    return list(rows)


async def direct_entry_counts(db: AsyncSession) -> dict[str, int]:
    """Count of live entries directly attached to each catalog. Used by
    the list_catalogs agent tool."""
    rows = (
        await db.execute(
            select(FileEntry.catalog_id, func.count())
            .where(
                FileEntry.catalog_id.isnot(None),
                FileEntry.deleted_at.is_(None),
            )
            .group_by(FileEntry.catalog_id)
        )
    ).all()
    return {cid: c for cid, c in rows}


async def list_live_direct_entries(
    db: AsyncSession, catalog_id: str,
    *, limit: int, offset: int = 0,
) -> list[FileEntry]:
    """Live entries attached directly (not transitively) to a catalog node,
    most-recently-updated first. Used by read_catalog."""
    stmt = (
        select(FileEntry)
        .where(
            FileEntry.catalog_id == catalog_id,
            FileEntry.deleted_at.is_(None),
        )
        .order_by(FileEntry.updated_at.desc())
    )
    if offset:
        stmt = stmt.offset(offset)
    stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def count_live_direct_entries(
    db: AsyncSession, catalog_id: str,
) -> int:
    """Total live entries directly attached to `catalog_id`."""
    stmt = select(func.count()).select_from(FileEntry).where(
        FileEntry.catalog_id == catalog_id,
        FileEntry.deleted_at.is_(None),
    )
    return int((await db.execute(stmt)).scalar_one())


async def find_live_child_by_name(
    db: AsyncSession, *, parent_id: str | None, name: str,
) -> Catalog | None:
    """Live catalog row matching `(parent_id, name)`. Used by ingest_file
    when materialising an LLM-suggested catalog path one segment at a time."""
    return (
        await db.execute(
            select(Catalog).where(
                Catalog.parent_id.is_(None) if parent_id is None
                else Catalog.parent_id == parent_id,
                Catalog.name == name,
                Catalog.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def list_live_id_parent(
    db: AsyncSession,
) -> list[tuple[str, str | None]]:
    """`(id, parent_id)` for every live catalog."""
    rows = (
        await db.execute(
            select(Catalog.id, Catalog.parent_id)
            .where(Catalog.deleted_at.is_(None))
        )
    ).all()
    return [(cid, pid) for cid, pid in rows]


async def list_live_sketch(
    db: AsyncSession, *, limit: int,
) -> list[tuple[str, str, str | None]]:
    """`(id, name, parent_id)` for up to `limit` live catalogs. Used by
    ingest_file to feed the pipeline a small catalog sketch."""
    rows = (
        await db.execute(
            select(Catalog.id, Catalog.name, Catalog.parent_id)
            .where(Catalog.deleted_at.is_(None))
            .limit(limit)
        )
    ).all()
    return [(cid, n, pid) for cid, n, pid in rows]


async def direct_entry_counts_for_live_catalogs(
    db: AsyncSession,
) -> dict[str, int]:
    """Same shape as `direct_entry_counts` but only counts entries attached
    to a non-NULL catalog_id. Used by restructure_catalogs for the snapshot."""
    rows = (
        await db.execute(
            select(FileEntry.catalog_id, func.count())
            .where(
                FileEntry.catalog_id.isnot(None),
                FileEntry.deleted_at.is_(None),
            )
            .group_by(FileEntry.catalog_id)
        )
    ).all()
    return {cid: int(c) for cid, c in rows}


async def list_live_children_of(
    db: AsyncSession, catalog_id: str,
) -> list[Catalog]:
    """Live children whose `parent_id` equals `catalog_id`. Used by the
    restructure_catalogs apply step when a parent is soft-deleted."""
    rows = (
        await db.execute(
            select(Catalog).where(
                Catalog.parent_id == catalog_id,
                Catalog.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def reassign_entries_catalog(
    db: AsyncSession, *, from_catalog_id: str, to_catalog_id: str | None,
    now: datetime,
) -> int:
    """Update file_entries.catalog_id for every row pointing at `from_catalog_id`.
    Used by restructure_catalogs apply when soft-deleting a parent and merging
    its entries into a target (or NULL = uncategorised). Returns row count."""
    result = await db.execute(
        update(FileEntry)
        .where(FileEntry.catalog_id == from_catalog_id)
        .values(catalog_id=to_catalog_id, updated_at=now)
    )
    return int(result.rowcount or 0)


async def move_entry_to_catalog(
    db: AsyncSession, *, entry_id: str, catalog_id: str | None, now: datetime,
) -> int:
    """Single-entry catalog move — used by restructure_catalogs' move_entries
    op so we can count rows actually moved."""
    result = await db.execute(
        update(FileEntry)
        .where(FileEntry.id == entry_id, FileEntry.deleted_at.is_(None))
        .values(catalog_id=catalog_id, updated_at=now)
    )
    return int(result.rowcount or 0)
