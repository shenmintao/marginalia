"""search_metadata — DESIGN.md §10.1.

Filters entries by combinations of text (ILIKE on summary + extras), tags,
catalog scope, view, kind, lifecycle. The two catalog filters are mutually
exclusive: `catalog_id` (single node, exact match) XOR `catalog_subtree`
(recursive). Returns minimal entry rows.
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

    catalog_in: list[str] | None = None
    if cat_subtree:
        catalog_in = await catalogs_repo.expand_subtree(db, cat_subtree)
        if not catalog_in:
            return {"entries": [], "count": 0}

    extra_ids: list[str] | None = None
    if view_id:
        view = await db.get(View, view_id)
        if view is None:
            return {"error": "view not found", "view_id": view_id}
        spec = view.filter_spec or {}
        extra_ids = await entries_repo.list_ids_under_filter_spec(
            db, spec,
            default_lifecycle=lifecycle,
            catalog_subtree_expander=lambda rid: catalogs_repo.expand_subtree(db, rid),
        )
        if not extra_ids:
            return {"entries": [], "count": 0}

    rows = await entries_repo.search_filtered(
        db,
        text=text_q,
        lifecycle=lifecycle,
        kind=kind,
        catalog_one=cat_one,
        catalog_in=catalog_in,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        extra_entry_ids=extra_ids,
        limit=limit,
    )

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
