"""list_folders — design.md §10.1.

Walks the user's folder tree. Folders' `name` is a soft prior signal for the
agent (the user named them).
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Folder


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "parent_id": {
            "type": ["string", "null"],
            "description": "Folder id whose direct children to list. Null = root folders.",
        },
    },
}


@tool(
    name="list_folders",
    description=(
        "List a folder's direct child folders (or root folders when "
        "parent_id is null). Use to walk the user's organisation as a soft "
        "prior signal — folder names are user-curated naming."
    ),
    schema=SCHEMA,
)
async def list_folders(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    parent_id = args.get("parent_id")
    stmt = select(Folder).where(Folder.deleted_at.is_(None))
    if parent_id is None:
        stmt = stmt.where(Folder.parent_id.is_(None))
    else:
        stmt = stmt.where(Folder.parent_id == parent_id)
    stmt = stmt.order_by(Folder.name)
    rows = (await db.execute(stmt)).scalars().all()
    return {
        "folders": [
            {
                "id": f.id,
                "parent_id": f.parent_id,
                "name": f.name,
                "created_at": f.created_at.isoformat() if f.created_at else None,
            }
            for f in rows
        ],
        "count": len(rows),
    }
