"""search_metadata — DESIGN.md §10.1.

Filters entries by combinations of text (ILIKE on display name, summary, and
extras), tags, catalog scope, folder scope, view, kind, lifecycle. Catalog
filters (`catalog_id` / `catalog_subtree`) are mutually exclusive; folder
filters (`folder_id` / `folder_subtree`) are mutually exclusive. Catalog and
folder filters can be combined (AND). Returns minimal entry rows + pagination
metadata (`total`, `has_more`, `next_offset`).
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import View
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import folders as folders_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "text": {
            "type": "string",
            "description": (
                "Free text matched against display_name, files.summary, "
                "files.extra, and entry.extra (ILIKE)."
            ),
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
        "folder_id": {
            "type": "string",
            "description": "Single folder match. Mutually exclusive with folder_subtree.",
        },
        "folder_subtree": {
            "type": "string",
            "description": "Folder id whose subtree (incl. self) the entry must fall in. Mutually exclusive with folder_id.",
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
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Skip first N matches (default 0). Use with `next_offset` to page.",
        },
    },
}


@tool(
    name="search_metadata",
    description=(
        "Narrow down candidate entries via filters. Tag ids must be already "
        "resolved (use resolve_tag). Catalog filters: catalog_id picks one "
        "node only; catalog_subtree picks the node and all descendants — "
        "mutually exclusive. Folder filters work the same way "
        "(folder_id / folder_subtree, mutually exclusive). Catalog and "
        "folder filters AND together. Pass `offset` (with the previous "
        "call's `next_offset`) to page beyond `limit`; the response carries "
        "`total` and `has_more`. Rows include compact coverage metadata when "
        "available."
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
    fld_one = args.get("folder_id")
    fld_subtree = args.get("folder_subtree")
    view_id = args.get("view_id")
    kind = args.get("kind")
    lifecycle = args.get("lifecycle") or ["active", "manual_active"]
    limit = min(int(args.get("limit") or 50), 500)
    offset = max(0, int(args.get("offset") or 0))

    if cat_one and cat_subtree:
        return {"error": "catalog_id and catalog_subtree are mutually exclusive"}
    if fld_one and fld_subtree:
        return {"error": "folder_id and folder_subtree are mutually exclusive"}

    catalog_in: list[str] | None = None
    if cat_subtree:
        catalog_in = await catalogs_repo.expand_subtree(db, cat_subtree)
        if not catalog_in:
            return _empty_page(limit, offset)

    folder_in: list[str] | None = None
    if fld_subtree:
        folder_in = await folders_repo.expand_subtree(db, fld_subtree)
        if not folder_in:
            return _empty_page(limit, offset)

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
            return _empty_page(limit, offset)

    common_filters = dict(
        text=text_q,
        lifecycle=lifecycle,
        kind=kind,
        catalog_one=cat_one,
        catalog_in=catalog_in,
        folder_one=fld_one,
        folder_in=folder_in,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        extra_entry_ids=extra_ids,
    )

    total = await entries_repo.count_filtered(db, **common_filters)
    rows = await entries_repo.search_filtered(
        db, **common_filters, limit=limit, offset=offset,
    )

    has_more = (offset + len(rows)) < total
    out: dict[str, Any] = {
        "entries": [_entry_row(e, f) for e, f in rows],
        "count": len(rows),
        "total": total,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(rows)
    return out


def _entry_row(entry: Any, file_row: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "entry_id": entry.id,
        "display_name": entry.display_name,
        "lifecycle": entry.lifecycle,
        "kind": file_row.kind,
        "summary": file_row.summary,
        "catalog_id": entry.catalog_id,
        "folder_id": entry.folder_id,
    }
    coverage = _compact_coverage(file_row.description)
    if coverage is not None:
        row["coverage"] = coverage
    return row


def _compact_coverage(description: Any) -> dict[str, Any] | None:
    if not isinstance(description, dict):
        return None
    coverage = description.get("coverage")
    if not isinstance(coverage, dict):
        return None
    keys = (
        "unit",
        "total_pages",
        "indexed_pages",
        "total_units",
        "indexed_units",
        "indexed_partial",
        "partial_reasons",
        "max_index_pages",
        "chunked",
        "chunk_count",
        "text_truncated",
        "truncated_chunks",
        "ocr_used",
        "ocr_pages_done",
    )
    compact = {key: coverage[key] for key in keys if key in coverage}
    return compact or None


def _empty_page(limit: int, offset: int) -> dict[str, Any]:
    return {
        "entries": [],
        "count": 0,
        "total": 0,
        "has_more": False,
    }
