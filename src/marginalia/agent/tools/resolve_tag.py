"""resolve_tag — design.md §10.1.

Maps any spelling (incl. aliases) to a canonical tag id + facet. Walks
tag_aliases when no direct hit, and follows tags.alias_of one step.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Tag, TagAlias


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": {
            "type": "string",
            "description": "Tag name written as the user / agent guesses it.",
        },
        "facet": {
            "type": "string",
            "enum": ["topic", "form", "time", "source", "language", "extra"],
            "description": "Optional facet filter to disambiguate.",
        },
    },
}


@tool(
    name="resolve_tag",
    description=(
        "Resolve a free-text tag name to a canonical tag id. Falls back to "
        "tag_aliases if no direct hit, and follows alias_of one step. Returns "
        "null when the tag does not exist (the agent should NOT coin new tags)."
    ),
    schema=SCHEMA,
)
async def resolve_tag(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    name = args["name"]
    facet = args.get("facet")

    # 1. direct match in tags
    stmt = select(Tag).where(Tag.name == name)
    if facet is not None:
        stmt = stmt.where(Tag.facet == facet)
    direct = (await db.execute(stmt)).scalars().all()
    for t in direct:
        canonical = t
        if t.alias_of is not None:
            canon = await db.get(Tag, t.alias_of)
            if canon is not None and canon.alias_of is None:
                canonical = canon
        return {
            "found": True,
            "via": "tags",
            "id": canonical.id,
            "name": canonical.name,
            "facet": canonical.facet,
            "doc_count": canonical.doc_count or 0,
            "was_alias": t.alias_of is not None,
        }

    # 2. fallback through tag_aliases
    alias_rows = (
        await db.execute(
            select(TagAlias).where(TagAlias.from_name == name)
        )
    ).scalars().all()
    for ar in alias_rows:
        canon = await db.get(Tag, ar.to_tag_id)
        if canon is None:
            continue
        if canon.alias_of is not None:
            root = await db.get(Tag, canon.alias_of)
            if root is not None and root.alias_of is None:
                canon = root
        if facet is not None and canon.facet != facet:
            continue
        return {
            "found": True,
            "via": "tag_aliases",
            "id": canon.id,
            "name": canon.name,
            "facet": canon.facet,
            "doc_count": canon.doc_count or 0,
            "was_alias": True,
        }

    return {"found": False, "name": name, "facet": facet}
