"""read_catalog — DESIGN.md §10.1.

Returns a catalog node's full metadata + its direct children + sample
entries linked to this node directly. Both children and entries paginate
under the unified contract.
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
        "children_limit": {
            "type": "integer", "minimum": 0, "maximum": 200,
            "description": "Cap on direct child catalogs returned. Default 50.",
        },
        "children_offset": {
            "type": "integer", "minimum": 0,
            "description": "Skip first N child catalogs. Default 0.",
        },
        "entries_limit": {
            "type": "integer", "minimum": 0, "maximum": 100,
            "description": "Cap on direct entries returned. Default 20.",
        },
        "entries_offset": {
            "type": "integer", "minimum": 0,
            "description": "Skip first N entries. Default 0.",
        },
    },
}


@tool(
    name="read_catalog",
    description=(
        "Read one catalog node's full metadata: summary, description, extra, "
        "tags, direct child catalogs, and a sample of entries directly linked "
        "to this node. Both lists paginate via `children_limit`/`children_offset` "
        "and `entries_limit`/`entries_offset`. Use after list_catalogs to "
        "drill into a node."
    ),
    schema=SCHEMA,
)
async def read_catalog(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    cat_id = args["id"]
    children_limit = min(int(args.get("children_limit") or 50), 200)
    children_offset = max(0, int(args.get("children_offset") or 0))
    entries_limit = min(int(args.get("entries_limit") or 20), 100)
    entries_offset = max(0, int(args.get("entries_offset") or 0))

    cat = await catalogs_repo.get_live(db, cat_id)
    if cat is None:
        return {"error": "catalog not found or deleted", "id": cat_id}

    children_total = await catalogs_repo.count_live_children(db, cat_id)
    children = await catalogs_repo.list_live_children(
        db, cat_id, limit=children_limit, offset=children_offset,
    ) if children_limit else []

    entries_total = await catalogs_repo.count_live_direct_entries(db, cat_id)
    entries = await catalogs_repo.list_live_direct_entries(
        db, cat_id, limit=entries_limit, offset=entries_offset,
    ) if entries_limit else []

    children_has_more = (children_offset + len(children)) < children_total
    entries_has_more = (entries_offset + len(entries)) < entries_total

    out: dict[str, Any] = {
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
        "children_total": children_total,
        "entries_total": entries_total,
        "children_has_more": children_has_more,
        "entries_has_more": entries_has_more,
    }
    if children_has_more:
        out["children_next_offset"] = children_offset + len(children)
    if entries_has_more:
        out["entries_next_offset"] = entries_offset + len(entries)
    return out
