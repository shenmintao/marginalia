"""resolve_tag — DESIGN.md §10.1.

Maps any spelling (incl. aliases) to a canonical tag id + facet. Walks
tag_aliases when no direct hit, and follows tags.alias_of one step.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Tag
from marginalia.repositories import tags as tags_repo


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
        "Low-level tag resolver for focused follow-up and precise tag-id "
        "filters. Returns null when the tag does not exist; do not coin new "
        "tags."
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

    direct = await tags_repo.find_by_name(db, name, facet=facet)
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

    alias_rows = await tags_repo.list_aliases_from(db, name)
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
