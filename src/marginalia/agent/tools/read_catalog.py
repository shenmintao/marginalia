"""read_catalog — DESIGN.md §10.1.

Returns a catalog node's full metadata + its direct children + sample
entries linked to this node directly.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.repositories import catalogs as catalogs_repo


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
    cat = await catalogs_repo.get_live(db, cat_id)
    if cat is None:
        return {"error": "catalog not found or deleted", "id": cat_id}

    children = await catalogs_repo.list_live_children(db, cat_id)
    entries = await catalogs_repo.list_live_direct_entries(
        db, cat_id, limit=entries_limit,
    )

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
