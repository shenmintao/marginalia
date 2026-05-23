"""read_entries_metadata — design.md §10.1.

Batch-fetches full metadata for a set of entry_ids and automatically attaches
`related_entries` derived from entry_relations (top by observation_count).

This is the canonical "agent has a list of candidates, now needs detail"
endpoint. Pair with read_files when the agent decides to crack one open.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import (
    Catalog,
    EntryRelation,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Tag,
)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_ids"],
    "properties": {
        "entry_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of entry ids to fetch.",
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
        "Batch-fetch full metadata for one or more entries: file summary + "
        "description + extra + tags + catalog path + per-entry extra, plus "
        "automatically-attached `related_entries` ranked by observation_count "
        "from entry_relations. Use to triage candidates before reading file "
        "bodies."
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

    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(FileEntry.id.in_(entry_ids))
        )
    ).all()

    out: list[dict[str, Any]] = []
    for entry, file_row in rows:
        # tags
        tag_rows = (
            await db.execute(
                select(Tag.id, Tag.name, Tag.facet, EntryTag.source)
                .join(EntryTag, Tag.id == EntryTag.tag_id)
                .where(EntryTag.entry_id == entry.id)
            )
        ).all()

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
            rel_rows = (
                await db.execute(
                    select(EntryRelation)
                    .where(or_(
                        EntryRelation.entry_a_id == entry.id,
                        EntryRelation.entry_b_id == entry.id,
                    ))
                    .order_by(
                        EntryRelation.observation_count.desc(),
                        EntryRelation.last_observed_at.desc(),
                    )
                    .limit(related_limit)
                )
            ).scalars().all()
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

    return {"entries": out, "count": len(out)}
