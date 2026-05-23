"""read_catalog — design.md §10.1.

Returns a catalog node's full metadata + its direct children + sample
entries linked to this node directly.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Catalog, FileEntry


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id"],
    "properties": {
        "id": {"type": "string"},
        "entries_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 100,
            "description": "Cap on direct entries returned. Default 20.",
        },
    },
}


@tool(
    name="read_catalog",
    description=(
        "Read one catalog node's full metadata: summary, description, extra, "
        "tags, direct child catalogs, and a sample of entries directly linked "
        "to this node. Use after list_catalogs to drill into a node."
    ),
    schema=SCHEMA,
)
async def read_catalog(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    cat_id = args["id"]
    entries_limit = min(int(args.get("entries_limit") or 20), 100)
    cat = await db.get(Catalog, cat_id)
    if cat is None or cat.deleted_at is not None:
        return {"error": "catalog not found or deleted", "id": cat_id}

    children = (
        await db.execute(
            select(Catalog)
            .where(Catalog.parent_id == cat_id, Catalog.deleted_at.is_(None))
            .order_by(Catalog.name)
        )
    ).scalars().all()

    entries = (
        await db.execute(
            select(FileEntry)
            .where(
                FileEntry.catalog_id == cat_id,
                FileEntry.deleted_at.is_(None),
            )
            .order_by(FileEntry.updated_at.desc())
            .limit(entries_limit)
        )
    ).scalars().all()

    return {
        "id": cat.id,
        "parent_id": cat.parent_id,
        "name": cat.name,
        "summary": cat.summary,
        "description": cat.description,
        "extra": cat.extra,
        "tags": cat.tags,
        "children": [
            {"id": c.id, "name": c.name, "summary": c.summary}
            for c in children
        ],
        "entries": [
            {
                "entry_id": e.id,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "extra": e.extra,
            }
            for e in entries
        ],
    }
