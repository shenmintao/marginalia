"""folders repository — pure SA queries against the Folder table.

The service layer (services/folders.py) handles business rules
(cycle detection, name-conflict policy, audit events). This module
exposes the lookup primitives those rules build on.

Caller owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import FileEntry, Folder


async def get_live(db: AsyncSession, folder_id: str) -> Folder | None:
    """Return the folder iff it exists and is not soft-deleted."""
    return (
        await db.execute(
            select(Folder).where(
                Folder.id == folder_id,
                Folder.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def expand_subtree(db: AsyncSession, root_id: str) -> list[str]:
    """BFS-collect ids of `root_id` plus every live folder beneath it.

    Mirror of catalogs_repo.expand_subtree. Used by search_metadata when
    the agent passes folder_subtree to scope candidate entries to a
    folder branch.
    """
    seen: set[str] = {root_id}
    frontier: list[str] = [root_id]
    while frontier:
        children = (
            await db.execute(
                select(Folder.id).where(
                    Folder.parent_id.in_(frontier),
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        new = [c for c in children if c not in seen]
        if not new:
            break
        seen.update(new)
        frontier = new
    return list(seen)


async def find_by_name(
    db: AsyncSession, name: str,
) -> list[Folder]:
    """All live folders with exact name `name` (root-level or nested).

    Returns a list because the same name can appear in different parents.
    Most common case: one match, but the caller must decide how to handle
    ambiguity.
    """
    stmt = (
        select(Folder)
        .where(
            Folder.name == name,
            Folder.deleted_at.is_(None),
        )
        .order_by(Folder.name)
    )
    return list((await db.execute(stmt)).scalars().all())


async def find_child_by_name(
    db: AsyncSession, *, parent_id: str | None, name: str,
) -> Folder | None:
    """Live folder with `name` directly under `parent_id` (None = root)."""
    stmt = select(Folder).where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
        Folder.name == name,
        Folder.deleted_at.is_(None),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_children(
    db: AsyncSession, parent_id: str | None,
    *, limit: int | None = None, offset: int = 0,
) -> list[Folder]:
    """Live children of `parent_id` (None = root), ordered by name.

    `limit` and `offset` are optional — omit for the legacy "all rows"
    behavior used by service-layer code that needs a complete list (e.g.
    cycle-detection, ambiguity hints). Agent tools should pass both."""
    stmt = (
        select(Folder)
        .where(
            Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
            Folder.deleted_at.is_(None),
        )
        .order_by(Folder.name)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def count_children(
    db: AsyncSession, parent_id: str | None,
) -> int:
    """Total live children of `parent_id`. Pair with paginated list_children."""
    stmt = select(func.count()).select_from(Folder).where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
        Folder.deleted_at.is_(None),
    )
    return int((await db.execute(stmt)).scalar_one())


async def find_sibling_id_by_name(
    db: AsyncSession,
    *,
    parent_id: str | None,
    name: str,
    exclude_id: str | None,
) -> str | None:
    """Used by rename/move: id of any other live sibling with the same name."""
    stmt = select(Folder.id).where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
        Folder.name == name,
        Folder.deleted_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Folder.id != exclude_id)
    return (await db.execute(stmt.limit(1))).scalar_one_or_none()


async def list_live_children_of_many(
    db: AsyncSession, parent_ids: list[str],
) -> list[Folder]:
    """Live folders whose parent_id is in `parent_ids`. Returns Folder rows
    (not just ids) so callers can build relative paths during a BFS walk."""
    if not parent_ids:
        return []
    rows = (
        await db.execute(
            select(Folder).where(
                Folder.parent_id.in_(parent_ids),
                Folder.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def list_live_descendant_ids(
    db: AsyncSession, root_id: str,
) -> list[str]:
    """BFS-collect ids of `root_id` plus every live folder beneath it."""
    out: list[str] = [root_id]
    frontier: list[str] = [root_id]
    while frontier:
        children = (
            await db.execute(
                select(Folder.id).where(
                    Folder.parent_id.in_(frontier),
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not children:
            break
        out.extend(children)
        frontier = list(children)
    return out


async def list_live_entries_in(
    db: AsyncSession, folder_ids: list[str],
) -> list[FileEntry]:
    """Live entries whose folder_id is in `folder_ids`."""
    if not folder_ids:
        return []
    return list(
        (
            await db.execute(
                select(FileEntry).where(
                    FileEntry.folder_id.in_(folder_ids),
                    FileEntry.deleted_at.is_(None),
                )
            )
        ).scalars().all()
    )


async def name_by_ids(
    db: AsyncSession, ids: list[str],
) -> dict[str, str]:
    """Map `folder_id -> name` for the given ids. Used by the agent
    runtime so tool_call display can render `list_folder Papers`
    instead of `list_folder 019e6339-…`. Includes soft-deleted folders
    so historical replay still resolves."""
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Folder.id, Folder.name).where(Folder.id.in_(ids))
        )
    ).all()
    return {fid: n for fid, n in rows}
