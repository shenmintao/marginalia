"""list_catalogs — design.md §10.1.

Walks the AI-internal catalog tree by parent. Soft-deleted nodes hidden.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Catalog, FileEntry


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
    stmt = select(Catalog).where(Catalog.deleted_at.is_(None))
    if parent_id is None:
        stmt = stmt.where(Catalog.parent_id.is_(None))
    else:
        stmt = stmt.where(Catalog.parent_id == parent_id)
    cats = (await db.execute(stmt.order_by(Catalog.name))).scalars().all()

    counts_rows = (
        await db.execute(
            select(FileEntry.catalog_id, func.count())
            .where(
                FileEntry.catalog_id.isnot(None),
                FileEntry.deleted_at.is_(None),
            )
            .group_by(FileEntry.catalog_id)
        )
    ).all()
    direct_counts = {cid: c for cid, c in counts_rows}

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
