"""list_files_in_folder — DESIGN.md §10.1.

Lists live entries inside a folder. Returns minimal fields the agent needs
to decide which entry to dive deeper on.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.repositories import entries as entries_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["folder_id"],
    "properties": {
        "folder_id": {
            "type": "string",
            "description": "Folder id to list entries from.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 200,
            "description": "Max entries returned. Default 50.",
        },
    },
}


@tool(
    name="list_files_in_folder",
    description=(
        "List the live (non-deleted) entries inside a given folder, with "
        "minimal metadata (display_name, lifecycle, mime_type, kind). Use to "
        "pick candidates for deeper inspection via read_entries_metadata or "
        "read_files."
    ),
    schema=SCHEMA,
)
async def list_files_in_folder(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    folder_id = args["folder_id"]
    limit = min(int(args.get("limit") or 50), 200)
    rows = await entries_repo.list_live_with_file_in_folder(
        db, folder_id, limit=limit,
    )

    return {
        "entries": [
            {
                "entry_id": e.id,
                "file_id": f.id,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "mime_type": f.mime_type,
                "kind": f.kind,
                "size_bytes": f.size_bytes,
                "ingest_status": f.ingest_status,
            }
            for e, f in rows
        ],
        "count": len(rows),
    }
