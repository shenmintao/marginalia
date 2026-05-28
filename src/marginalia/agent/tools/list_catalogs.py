"""list_catalogs — DESIGN.md §10.1.

Walks the AI-internal catalog tree by parent. Soft-deleted nodes hidden.
Paginates via `limit` + `offset`; output carries `total` / `has_more` /
`next_offset` per the unified pagination contract.
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
        "limit": {
            "type": "integer", "minimum": 1, "maximum": 500,
            "description": "Cap on returned children. Default 100.",
        },
        "offset": {
            "type": "integer", "minimum": 0,
            "description": "Skip first N children. Default 0.",
        },
    },
}


@tool(
    name="list_catalogs",
    description=(
        "List a catalog's direct child catalogs (or root catalogs when "
        "parent_id is null). Each entry includes summary + doc_count "
        "(live entries linked at any depth below). Paginates via "
        "`limit`/`offset`; response carries `total` and `next_offset`."
    ),
    schema=SCHEMA,
)
async def list_catalogs(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    parent_id = _coerce_parent_id(args.get("parent_id"))
    limit = min(int(args.get("limit") or 100), 500)
    offset = max(0, int(args.get("offset") or 0))

    total = await catalogs_repo.count_live_children(db, parent_id)
    cats = await catalogs_repo.list_live_children(
        db, parent_id, limit=limit, offset=offset,
    )
    direct_counts = await catalogs_repo.direct_entry_counts(db)

    has_more = (offset + len(cats)) < total
    out: dict[str, Any] = {
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
        "total": total,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(cats)
    return out


def _coerce_parent_id(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "root"}:
        return None
    return text
