"""read_entries_metadata — DESIGN.md §10.1.

Batch-fetches full metadata for a set of entry_ids and automatically attaches
`related_entries` derived from entry_relations (top by observation_count).

This is the canonical "agent has a list of candidates, now needs detail"
endpoint. Pair with read_files when the agent decides to crack one open.

`entry_ids` may include short hex prefixes (>= 8 chars); see
`entries_repo.resolve_entry_id_prefix`.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Catalog, Folder
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_relations as relations_repo
from marginalia.repositories import entry_tags as entry_tags_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_ids"],
    "properties": {
        "entry_ids": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
            "description": (
                "List of entry UUIDs (or short hex prefixes, ≥ 8 chars) "
                "to fetch (max 50). NOT file names — resolve via "
                "search_metadata / list_folder first. For more candidates, "
                "page via search_metadata.next_offset and call this tool "
                "again with the next batch."
            ),
        },
        "related_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 30,
            "description": "How many related entries (per entry) to attach. Default 10.",
        },
    },
}


@tool(
    name="read_entries_metadata",
    description=(
        "Batch-fetch full metadata for up to 50 entries in one call: file "
        "summary + description + extra + tags + catalog path + per-entry "
        "extra, plus automatically-attached `related_entries` ranked by "
        "observation_count from entry_relations. Use to triage candidates "
        "before reading file bodies. If you have more than 50 candidates, "
        "page through search_metadata first."
    ),
    schema=SCHEMA,
)
async def read_entries_metadata(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    entry_ids = list(args.get("entry_ids") or [])
    related_limit = min(int(args.get("related_limit") or 10), 30)
    if not entry_ids:
        return {"entries": [], "count": 0}

    # Resolve short prefixes to full uuids; collect ambiguous / unknown
    # ones in `errors` so the agent gets feedback rather than silently
    # dropping them.
    resolved_ids: list[str] = []
    errors: list[dict[str, str]] = []
    for raw in entry_ids:
        s = (raw or "").strip() if isinstance(raw, str) else ""
        if not s:
            continue
        full, err = await entries_repo.resolve_entry_id_prefix(db, s)
        if err:
            errors.append({"entry_id": s, "error": err})
            continue
        resolved_ids.append(full)

    rows = await entries_repo.list_with_file_by_ids_any(db, resolved_ids)

    out: list[dict[str, Any]] = []
    for entry, file_row in rows:
        # tags
        tag_rows = await entry_tags_repo.list_tags_with_source_for_entry(
            db, entry.id,
        )

        # catalog path
        catalog_path: list[dict[str, Any]] = []
        cur_id = entry.catalog_id
        while cur_id:
            cat = await db.get(Catalog, cur_id)
            if cat is None or cat.deleted_at is not None:
                break
            catalog_path.append({"id": cat.id, "name": cat.name})
            cur_id = cat.parent_id
        catalog_path.reverse()

        # folder path (user-side, soft prior signal)
        folder_path: list[dict[str, Any]] = []
        cur_fid = entry.folder_id
        while cur_fid:
            fld = await db.get(Folder, cur_fid)
            if fld is None or fld.deleted_at is not None:
                break
            folder_path.append({"id": fld.id, "name": fld.name})
            cur_fid = fld.parent_id
        folder_path.reverse()

        # related_entries
        related: list[dict[str, Any]] = []
        if related_limit:
            rel_rows = await relations_repo.list_top_for_entry(
                db, entry.id, limit=related_limit,
            )
            for r in rel_rows:
                other_id = r.entry_b_id if r.entry_a_id == entry.id else r.entry_a_id
                related.append({
                    "entry_id": other_id,
                    "note": r.note,
                    "source_kind": r.source_kind,
                    "observation_count": r.observation_count,
                    "last_observed_at": (
                        r.last_observed_at.isoformat() if r.last_observed_at else None
                    ),
                })

        out.append({
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "extra": entry.extra,
            "folder_path": folder_path,
            "catalog_path": catalog_path,
            "tags": [
                {"id": tid, "name": n, "facet": f, "source": src}
                for tid, n, f, src in tag_rows
            ],
            "file": {
                "file_id": file_row.id,
                "kind": file_row.kind,
                "summary": file_row.summary,
                "description": file_row.description,
                "extra": file_row.extra,
                "mime_type": file_row.mime_type,
                "ingest_status": file_row.ingest_status,
            },
            "related_entries": related,
        })

    result: dict[str, Any] = {"entries": out, "count": len(out)}
    if errors:
        result["errors"] = errors
    return result
