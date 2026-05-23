"""search_metadata — design.md §10.1.

Filters entries by combinations of text (ILIKE on summary + extras), tags,
catalog scope, view, kind, lifecycle. The two catalog filters are mutually
exclusive: `catalog_id` (single node, exact match) XOR `catalog_subtree`
(recursive). Returns minimal entry rows.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Catalog, EntryTag, File, FileEntry, View


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "text": {
            "type": "string",
            "description": "Free text matched against files.summary + extras (ILIKE).",
        },
        "tags_all": {"type": "array", "items": {"type": "string"}},
        "tags_any": {"type": "array", "items": {"type": "string"}},
        "tags_none": {"type": "array", "items": {"type": "string"}},
        "catalog_id": {
            "type": "string",
            "description": "Single catalog match. Mutually exclusive with catalog_subtree.",
        },
        "catalog_subtree": {
            "type": "string",
            "description": "Catalog id whose subtree (incl. self) the entry must fall in. Mutually exclusive with catalog_id.",
        },
        "view_id": {
            "type": "string",
            "description": "Restrict to entries already inside this view's filter_spec.",
        },
        "kind": {"type": "string"},
        "lifecycle": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["active", "demoted", "archived", "manual_active", "manual_archived"],
            },
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
    },
}


@tool(
    name="search_metadata",
    description=(
        "Narrow down candidate entries via filters. Tag ids must be already "
        "resolved (use resolve_tag). Catalog filters: catalog_id picks one "
        "node only; catalog_subtree picks the node and all descendants — "
        "they are mutually exclusive."
    ),
    schema=SCHEMA,
)
async def search_metadata(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    text_q = (args.get("text") or "").strip() or None
    tags_all = args.get("tags_all") or []
    tags_any = args.get("tags_any") or []
    tags_none = args.get("tags_none") or []
    cat_one = args.get("catalog_id")
    cat_subtree = args.get("catalog_subtree")
    view_id = args.get("view_id")
    kind = args.get("kind")
    lifecycle = args.get("lifecycle") or ["active", "manual_active"]
    limit = min(int(args.get("limit") or 50), 500)

    if cat_one and cat_subtree:
        return {"error": "catalog_id and catalog_subtree are mutually exclusive"}

    stmt = (
        select(FileEntry, File)
        .join(File, File.id == FileEntry.file_id)
        .where(
            FileEntry.deleted_at.is_(None),
            File.deleted_at.is_(None),
            FileEntry.lifecycle.in_(lifecycle),
        )
    )
    if kind:
        stmt = stmt.where(File.kind == kind)

    if text_q:
        like = f"%{text_q}%"
        stmt = stmt.where(or_(
            File.summary.ilike(like),
            File.extra.ilike(like),
            FileEntry.extra.ilike(like),
            FileEntry.display_name.ilike(like),
        ))

    if cat_one:
        stmt = stmt.where(FileEntry.catalog_id == cat_one)
    elif cat_subtree:
        ids = await _expand_subtree(db, cat_subtree)
        if not ids:
            return {"entries": [], "count": 0}
        stmt = stmt.where(FileEntry.catalog_id.in_(ids))

    for tid in tags_all:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id == tid)
        stmt = stmt.where(FileEntry.id.in_(sub))
    if tags_any:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_any))
        stmt = stmt.where(FileEntry.id.in_(sub))
    if tags_none:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_none))
        stmt = stmt.where(not_(FileEntry.id.in_(sub)))

    if view_id:
        view = await db.get(View, view_id)
        if view is None:
            return {"error": "view not found", "view_id": view_id}
        spec = view.filter_spec or {}
        # Re-apply view's own filters as additional constraints. Cheap enough
        # because view filter logic is the same shape.
        sub_ids = (await _entries_under_filter_spec(db, spec, lifecycle))
        if not sub_ids:
            return {"entries": [], "count": 0}
        stmt = stmt.where(FileEntry.id.in_(sub_ids))

    rows = (
        await db.execute(
            stmt.order_by(FileEntry.updated_at.desc()).limit(limit)
        )
    ).all()

    return {
        "entries": [
            {
                "entry_id": e.id,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "kind": f.kind,
                "summary": f.summary,
                "catalog_id": e.catalog_id,
            }
            for e, f in rows
        ],
        "count": len(rows),
    }


async def _expand_subtree(db: AsyncSession, root: str) -> list[str]:
    seen = {root}
    frontier = [root]
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


async def _entries_under_filter_spec(
    db: AsyncSession, spec: dict[str, Any], lifecycle: list[str]
) -> list[str]:
    """A minimal duplicate of materialize_view's filter eval — keeps
    search_metadata self-contained."""
    stmt = (
        select(FileEntry.id)
        .where(FileEntry.deleted_at.is_(None))
        .where(FileEntry.lifecycle.in_(spec.get("lifecycle") or lifecycle))
    )
    sub = spec.get("catalog_subtree") or []
    if sub:
        ids: list[str] = []
        for r in sub:
            ids.extend(await _expand_subtree(db, r))
        if not ids:
            return []
        stmt = stmt.where(FileEntry.catalog_id.in_(ids))
    for tid in spec.get("tags_all") or []:
        s = select(EntryTag.entry_id).where(EntryTag.tag_id == tid)
        stmt = stmt.where(FileEntry.id.in_(s))
    if spec.get("tags_any"):
        s = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(spec["tags_any"]))
        stmt = stmt.where(FileEntry.id.in_(s))
    if spec.get("tags_none"):
        s = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(spec["tags_none"]))
        stmt = stmt.where(not_(FileEntry.id.in_(s)))
    return list((await db.execute(stmt)).scalars().all())
