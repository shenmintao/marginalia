"""list_folder — browse a folder's contents.

Returns both child folders and entries (files) at the requested level
in one call. The singular name emphasizes that this tool lists the
*contents of one folder*, not multiple folders.

Three ways to point at a level:
  - parent_id=<id>           direct id (UUID or short hex prefix)
  - path="Papers/2024"       slash-separated path resolved to a leaf
  - path="2024"              bare name → global find_by_name (must be unique)
  - (nothing)                root level

parent_id and path are mutually exclusive.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import folders as folders_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "parent_id": {
            "type": ["string", "null"],
            "description": "Folder id whose direct children to list. Null = root folders. Mutually exclusive with path.",
        },
        "path": {
            "type": "string",
            "description": (
                "Slash-separated folder path like 'Papers/2024', or a bare "
                "folder name. Resolved to a single folder; the call then "
                "lists that folder's child folders and entries. Mutually "
                "exclusive with parent_id."
            ),
        },
    },
}


async def _resolve_path(
    db: AsyncSession, raw: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a path string to a single folder id.

    Returns (folder_id, None) on success or (None, error_dict) on failure.
    Error dicts carry hints so the LLM can self-correct.
    """
    path = raw.strip().strip("/")
    if not path:
        return None, {"ok": False, "error": "path is empty",
                      "folders": [], "entries": [],
                      "folder_count": 0, "entry_count": 0}

    if "/" in path:
        segments = [s.strip() for s in path.split("/") if s.strip()]
        parent_id: str | None = None
        for i, seg in enumerate(segments):
            child = await folders_repo.find_child_by_name(
                db, parent_id=parent_id, name=seg,
            )
            if child is None:
                siblings = await folders_repo.list_children(db, parent_id)
                hint = ", ".join(s.name for s in siblings[:20])
                prefix = "/".join(segments[:i]) or "(root)"
                return None, {
                    "ok": False,
                    "error": (
                        f"no folder named '{seg}' under '{prefix}'. "
                        f"Available: {hint}"
                    ),
                    "folders": [], "entries": [],
                    "folder_count": 0, "entry_count": 0,
                }
            parent_id = child.id
        return parent_id, None

    matches = await folders_repo.find_by_name(db, path)
    if not matches:
        roots = await folders_repo.list_children(db, None)
        hint = ", ".join(r.name for r in roots[:20])
        return None, {
            "ok": False,
            "error": f"no folder named '{path}'. Available at root: {hint}",
            "folders": [], "entries": [],
            "folder_count": 0, "entry_count": 0,
        }
    if len(matches) > 1:
        names = ", ".join(
            f"{m.parent_id or 'root'}/{m.name}" for m in matches[:20]
        )
        return None, {
            "ok": False,
            "error": (
                f"folder name '{path}' is ambiguous "
                f"({len(matches)} matches). "
                f"Disambiguate with a full path: {names}"
            ),
            "folders": [], "entries": [],
            "folder_count": 0, "entry_count": 0,
        }
    return matches[0].id, None


@tool(
    name="list_folder",
    description=(
        "List ONE folder's contents: its direct child folders AND the "
        "entries (files) inside it. Pass parent_id=null (or omit) for "
        "root level, parent_id=<id> for a specific folder, or "
        "path='Papers/2024' to resolve by name. parent_id and path are "
        "mutually exclusive. Returns both folders and entries in one call."
    ),
    schema=SCHEMA,
)
async def list_folder(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    raw_parent = args.get("parent_id")
    raw_path = args.get("path")

    if raw_path and raw_parent:
        return {
            "ok": False,
            "error": "parent_id and path are mutually exclusive",
            "folders": [], "entries": [],
            "folder_count": 0, "entry_count": 0,
        }

    if raw_path:
        parent_id, err = await _resolve_path(db, str(raw_path))
        if err is not None:
            return err
    else:
        # LLMs sometimes pass the string "null" instead of JSON null.
        parent_id = (
            None if raw_parent is None or raw_parent == "null"
            else str(raw_parent)
        )

    folders = await folders_repo.list_children(db, parent_id)
    entries = await entries_repo.list_live_in_folder(db, parent_id)
    return {
        "folders": [
            {
                "id": f.id,
                "parent_id": f.parent_id,
                "name": f.name,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in folders
        ],
        "entries": [
            {
                "entry_id": e.id,
                "folder_id": e.folder_id,
                "file_id": e.file_id,
                "display_name": e.display_name,
                "lifecycle": e.lifecycle,
                "ingest_status": st,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e, st in entries
        ],
        "folder_count": len(folders),
        "entry_count": len(entries),
    }
