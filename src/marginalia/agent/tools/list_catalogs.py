"""list_catalogs — DESIGN.md §10.1.

Walks the AI-internal catalog tree by parent. Soft-deleted nodes hidden.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.repositories import catalogs as catalogs_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "parent_id": {
            "type": ["string", "null"],
            "description": "Catalog id whose direct children to list. Null = root.",
        },
    },
}


@tool(
    name="list_catalogs",
    description=(
        "List a catalog's direct child catalogs (or root catalogs when "
        "parent_id is null). Each entry includes summary + doc_count "
        "(live entries linked at any depth below)."
    ),
    schema=SCHEMA,
)
async def list_catalogs(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    parent_id = args.get("parent_id")
    cats = await catalogs_repo.list_live_children(db, parent_id)
    direct_counts = await catalogs_repo.direct_entry_counts(db)

    return {
        "catalogs": [
            {
                "id": c.id,
                "parent_id": c.parent_id,
                "name": c.name,
                "summary": c.summary,
                "tags": c.tags,
                "doc_count": direct_counts.get(c.id, 0),
            }
            for c in cats
        ],
        "count": len(cats),
    }
