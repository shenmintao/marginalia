"""search_journal — design.md §10.1.

The investigator's first move: "did I work on something like this before?"
Returns recent journal notes filtered by free-text / entry_id / tags / since.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Journal


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "text": {
            "type": "string",
            "description": "Free-text fragment to match in journal notes.",
        },
        "entry_id": {
            "type": "string",
            "description": "Only return notes whose entry_ids list includes this id.",
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Match notes carrying ALL of these tags.",
        },
        "since_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "description": "Limit to notes written within the last N days. Default 90.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max notes returned. Default 10.",
        },
        "order": {
            "type": "string",
            "enum": ["recent_first", "oldest_first"],
            "description": "Default 'recent_first'.",
        },
    },
}


@tool(
    name="search_journal",
    description=(
        "Skim your investigator's notebook for past notes related to the "
        "current question. Always your first move on a fresh user message — "
        "before reading any file."
    ),
    schema=SCHEMA,
)
async def search_journal(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    text_q = args.get("text")
    entry_id = args.get("entry_id")
    tags = args.get("tags") or []
    since_days = int(args.get("since_days") or 90)
    limit = min(int(args.get("limit") or 10), 50)
    order = args.get("order") or "recent_first"

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    stmt = select(Journal).where(Journal.created_at >= cutoff)
    if text_q:
        like = f"%{text_q}%"
        stmt = stmt.where(Journal.note.ilike(like))
    if order == "oldest_first":
        stmt = stmt.order_by(Journal.created_at.asc())
    else:
        stmt = stmt.order_by(Journal.created_at.desc())

    rows = (await db.execute(stmt.limit(limit * 4))).scalars().all()

    # entry_id and tags filters: SQLite JSON cannot be cleanly filtered
    # server-side, so we post-filter in Python (results capped above).
    filtered: list[Journal] = []
    for j in rows:
        if entry_id and entry_id not in (j.entry_ids or []):
            continue
        if tags:
            note_tags = set(j.tags or [])
            if not all(t in note_tags for t in tags):
                continue
        filtered.append(j)
        if len(filtered) >= limit:
            break

    return {
        "notes": [
            {
                "id": j.id,
                "conversation_id": j.conversation_id,
                "note": j.note,
                "entry_ids": list(j.entry_ids or []),
                "tags": list(j.tags or []),
                "source_kind": j.source_kind,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in filtered
        ],
        "count": len(filtered),
    }
