"""search_journal — DESIGN.md §10.1.

The investigator's first move: "did I work on something like this before?"

The journal is two tiers in one table (see [[journal-tiers]]):
  - `insight`: durable cross-session distillations (default kind).
  - `reflect_turn`: per-turn bullets from one specific session.

Defaults to `kinds=["insight"]` so a fresh user message goes straight to
durable memory. Pass `kinds=["reflect_turn"]` together with a
`conversation_id` to skim the per-turn bullets of one session.

Superseded insight rows (whose `superseded_by_id IS NOT NULL`) are hidden
by default — the chain replacement IS the answer. Set
`include_superseded=true` to see history.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import Journal
from marginalia.repositories import journal as journal_repo


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
            "description": (
                "Only return notes whose entry_ids list includes this id. "
                "Must be a UUID or short hex prefix (≥ 8 chars), NOT a "
                "file name."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Match notes carrying ANY of these tags (OR).",
        },
        "kinds": {
            "type": "array",
            "items": {"type": "string", "enum": ["insight", "reflect_turn"]},
            "description": (
                "Which journal tiers to search. Default "
                "['insight', 'reflect_turn']: both durable "
                "cross-session memory and per-turn bullets."
            ),
        },
        "conversation_id": {
            "type": "string",
            "description": "Restrict to notes attached to this conversation.",
        },
        "include_superseded": {
            "type": "boolean",
            "description": (
                "If true, include insight rows that have been replaced by a "
                "newer version. Default false — only the current version "
                "of each chain is returned."
            ),
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
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Skip first N matches that satisfy all filters (text + "
                "entry_id + any tag + kinds + conversation + since/super). "
                "Default 0. Use with `next_offset` to page."
            ),
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
        "Skim your investigator's notebook for past insights. Always your "
        "first move on a fresh user message — before reading any file. "
        "Searches both durable cross-session insights and per-turn "
        "reflect_turn notes by default."
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
    kinds = list(args.get("kinds") or ["insight", "reflect_turn"])
    conversation_id = args.get("conversation_id")
    include_superseded = bool(args.get("include_superseded") or False)
    since_days = int(args.get("since_days") or 90)
    limit = min(int(args.get("limit") or 10), 50)
    offset = max(0, int(args.get("offset") or 0))
    order = args.get("order") or "recent_first"

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)

    # The journal's JSON filters (entry_id + tags) cannot be expressed in
    # SQLite cleanly, so we post-filter in Python. To honor a true offset
    # we walk the SQL window forward (chunks of `limit*4`) until we have
    # collected `offset + limit` post-filtered hits or exhausted rows.
    needed = offset + limit
    collected: list[Journal] = []
    cursor = 0
    chunk = max(limit * 4, 20)
    exhausted = False
    while len(collected) < needed:
        rows = await journal_repo.search(
            db,
            cutoff=cutoff,
            kinds=kinds,
            conversation_id=conversation_id,
            include_superseded=include_superseded,
            text=text_q,
            order=order,
            limit=chunk,
            offset=cursor,
        )
        if not rows:
            exhausted = True
            break
        for j in rows:
            if entry_id and entry_id not in (j.entry_ids or []):
                continue
            if tags:
                note_tags = set(j.tags or [])
                if not note_tags.intersection(tags):
                    continue
            collected.append(j)
            if len(collected) >= needed:
                break
        cursor += len(rows)
        if len(rows) < chunk:
            exhausted = True
            break

    page = collected[offset: offset + limit]
    has_more = (not exhausted) or (len(collected) > offset + len(page))
    out: dict[str, Any] = {
        "notes": [
            {
                "id": j.id,
                "conversation_id": j.conversation_id,
                "note": j.note,
                "entry_ids": list(j.entry_ids or []),
                "tags": list(j.tags or []),
                "source_kind": j.source_kind,
                "superseded_by_id": j.superseded_by_id,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in page
        ],
        "count": len(page),
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(page)
    return out
