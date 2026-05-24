"""materialize_view — DESIGN.md §10.1.

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

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import View
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo


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

    lifecycle = spec.get("lifecycle") or ["active", "manual_active"]

    catalog_in: list[str] | None = None
    subtree = spec.get("catalog_subtree") or []
    if subtree:
        ids: list[str] = []
        for r in subtree:
            ids.extend(await catalogs_repo.expand_subtree(db, r))
        if not ids:
            return {"view_id": view_id, "name": v.name, "entries": [], "count": 0}
        catalog_in = ids

    rows = await entries_repo.search_filtered(
        db,
        lifecycle=lifecycle,
        kind=spec.get("kind"),
        catalog_in=catalog_in,
        tags_all=spec.get("tags_all") or [],
        tags_any=spec.get("tags_any") or [],
        tags_none=spec.get("tags_none") or [],
        limit=limit,
    )

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
