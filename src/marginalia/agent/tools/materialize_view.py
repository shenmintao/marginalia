"""materialize_view — design.md §10.1.

Realises a view's filter_spec into a concrete entry list. Supports:
  - catalog_subtree: list of catalog ids; entries whose catalog is any of
    these OR a descendant
  - tags_all / tags_any / tags_none: tag ids (already resolved upstream)
  - kind: file kind filter
  - lifecycle: list, default ('active', 'manual_active')
  - limit: cap on returned entries

Filter execution is intentionally simple — for V1, complex multi-filter
performance is not the bottleneck; corpus stays under 100k entries.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Catalog, EntryTag, File, FileEntry, View


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string", "description": "View id to materialise."},
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 500,
            "description": "Max entries returned. Default 50.",
        },
    },
}


@tool(
    name="materialize_view",
    description=(
        "Run a view's filter_spec to produce its current entry list. Use to "
        "check which entries currently match a saved view (e.g. a topic-aggregating "
        "view). Returns minimal metadata; pair with read_entries_metadata for detail."
    ),
    schema=SCHEMA,
)
async def materialize_view(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    view_id = args["id"]
    limit = min(int(args.get("limit") or 50), 500)

    v = await db.get(View, view_id)
    if v is None:
        return {"error": "view not found", "id": view_id}
    spec: dict[str, Any] = v.filter_spec or {}

    stmt = (
        select(FileEntry, File)
        .join(File, File.id == FileEntry.file_id)
        .where(
            FileEntry.deleted_at.is_(None),
            File.deleted_at.is_(None),
        )
    )

    # lifecycle (default active variants)
    lifecycle = spec.get("lifecycle") or ["active", "manual_active"]
    stmt = stmt.where(FileEntry.lifecycle.in_(lifecycle))

    # kind
    if spec.get("kind"):
        stmt = stmt.where(File.kind == spec["kind"])

    # catalog_subtree (recursive)
    subtree = spec.get("catalog_subtree") or []
    if subtree:
        all_cat_ids = await _expand_catalog_subtree(db, subtree)
        if not all_cat_ids:
            return {"view_id": view_id, "name": v.name, "entries": [], "count": 0}
        stmt = stmt.where(FileEntry.catalog_id.in_(all_cat_ids))

    # tags
    tags_all = spec.get("tags_all") or []
    for tid in tags_all:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id == tid)
        stmt = stmt.where(FileEntry.id.in_(sub))
    tags_any = spec.get("tags_any") or []
    if tags_any:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_any))
        stmt = stmt.where(FileEntry.id.in_(sub))
    tags_none = spec.get("tags_none") or []
    if tags_none:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_none))
        stmt = stmt.where(not_(FileEntry.id.in_(sub)))

    rows = (
        await db.execute(stmt.order_by(FileEntry.updated_at.desc()).limit(limit))
    ).all()

    return {
        "view_id": view_id,
        "name": v.name,
        "summary": v.summary,
        "entries": [
            {
                "entry_id": e.id,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "kind": f.kind,
                "summary": f.summary,
            }
            for e, f in rows
        ],
        "count": len(rows),
    }


async def _expand_catalog_subtree(
    db: AsyncSession, roots: list[str]
) -> list[str]:
    """Return ids of `roots` plus all descendants (live only)."""
    seen: set[str] = set(roots)
    frontier = list(roots)
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
