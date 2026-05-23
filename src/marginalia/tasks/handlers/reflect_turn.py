"""reflect_turn handler — design.md §9.4 + §12.3.

Identity: [🔍 investigator → 🏛️ librarian]. Investigator-style reasoning,
librarian-style writes. Only this handler may write to journal /
entry_relations / entry_tags(source='reflect') / file_entries.extra /
catalogs.extra / views.extra.

Inputs:
  payload = {"conversation_id": "..."}

Flow:
  1. Idempotence check: if a task_outcomes row exists for this conversation
     under task_kind='reflect_turn', short-circuit (no-op).
  2. Pull the conversation (full fact record: user_message, agent_response,
     tool_calls, llm_calls). Refuse to reflect on conversations not yet ended.
  3. Resolve the involved entry_ids (from tool_calls payload — the agent's
     read/write trail). For each, fetch current metadata: file_entry,
     associated file's summary/description/kind, current entry_tags, current
     pairwise entry_relations.
  4. Build the prompt and call the `reflect` LLM profile (strict JSON output).
  5. Single transaction:
     - INSERT journal rows
     - For entry_relations: ensure (a_id < b_id) ordering; INSERT-or-UPDATE
       (observation_count++, last_observed_at, optionally append note)
     - INSERT entry_tags rows with source='reflect' (skip if pair already
       exists)
     - UPDATE file_entries.extra (per `entry_extra_updates`)
     - UPDATE catalogs.extra (per `catalog_extra_updates`)
     - UPDATE views.extra (per `view_extra_updates`)
     - record_outcome(task_kind='reflect_turn', object_kind='conversation',
       object_id=conversation_id, outcome='applied', detail=counts)
  6. NEVER touch files.summary/description/extra/kind/ingested_at — design
     §14.2 guarantee.
  7. Audit reads: design §14.3 #2 forbids reading audit_events here. We use
     task_outcomes for the idempotence check.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, update

from marginalia.db.models import (
    Catalog,
    Conversation,
    EntryRelation,
    EntryTag,
    File,
    FileEntry,
    Journal,
    Tag,
    View,
)
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    get_chat_client,
)
from marginalia.services.audit import write_event
from marginalia.services.task_outcomes import has_outcome, record_outcome
from marginalia.tasks.kinds import task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

KIND_REFLECT_TURN = "reflect_turn"

ENTRY_LIMIT = 30  # cap how many entries we feed the model context for


REFLECT_SYSTEM = """You are Marginalia's reflection investigator.

You read one finished conversation between a user and the Marginalia agent —
along with the current metadata of the file_entries the agent touched — and
decide what is worth remembering for next time.

Your output drives the AI-internal memory layer. Be conservative: it is fine
to return nothing in any field, the framework will simply skip those writes.
Do not invent entries, files, or relations that were not in the conversation.

Six independent things you may produce (any or none of them):

1. journal_entries — short field notes, one self-contained insight per entry.
   Each note answers: "what would I want my future self to find when looking
   for similar work?" Tie to specific entry_ids if the insight is about them.
   You may use tags=["hint:restructure_catalogs"] etc. to leave hints for
   offline maintenance tasks.

2. entry_relations — pairwise associations between two entries. Use whenever
   the conversation revealed two entries belong together, contradict, build
   on each other, or share a deeper purpose. The framework will dedupe pairs
   and increment observation_count if a pair was seen before.

3. entry_tag_additions — new (entry_id, tag) pairs you want attached. Use
   when the conversation revealed a missing tag — e.g. you discovered a doc
   is actually about "consensus algorithms" but it lacked that tag.

4. entry_extra_updates — overwrite the entry's accumulated insight (free
   text). Use when you want to refresh the agent's working memory of WHAT
   THIS ENTRY MEANS at this position right now. Empty string means clear.

5. catalog_extra_updates / view_extra_updates — same idea but for catalogs
   or views the conversation touched.

Output ONLY one JSON object matching the supplied schema. No prose, no fences.
"""


REFLECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "journal_entries",
        "entry_relations",
        "entry_tag_additions",
        "entry_extra_updates",
        "catalog_extra_updates",
        "view_extra_updates",
    ],
    "properties": {
        "journal_entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["note", "entry_ids", "tags"],
                "properties": {
                    "note": {"type": "string"},
                    "entry_ids": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "entry_relations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_a_id", "entry_b_id", "note"],
                "properties": {
                    "entry_a_id": {"type": "string"},
                    "entry_b_id": {"type": "string"},
                    "note": {"type": "string"},
                },
            },
        },
        "entry_tag_additions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id", "name", "facet"],
                "properties": {
                    "entry_id": {"type": "string"},
                    "name": {"type": "string"},
                    "facet": {
                        "type": "string",
                        "enum": ["topic", "form", "time", "source", "language", "extra"],
                    },
                },
            },
        },
        "entry_extra_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id", "extra"],
                "properties": {
                    "entry_id": {"type": "string"},
                    "extra": {"type": "string"},
                },
            },
        },
        "catalog_extra_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["catalog_id", "extra"],
                "properties": {
                    "catalog_id": {"type": "string"},
                    "extra": {"type": "string"},
                },
            },
        },
        "view_extra_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["view_id", "extra"],
                "properties": {
                    "view_id": {"type": "string"},
                    "extra": {"type": "string"},
                },
            },
        },
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_REFLECT_TURN)
async def handle_reflect_turn(payload: Mapping[str, Any]) -> None:
    conversation_id = payload.get("conversation_id")
    if not conversation_id:
        raise ValueError("reflect_turn payload missing conversation_id")

    # --- 1. idempotence: skip if task_outcomes row exists for this conv.
    #         (design.md §14.3 — never read audit_events for business logic.)
    async with session_scope() as session:
        already = await has_outcome(
            session,
            task_kind="reflect_turn",
            object_kind="conversation",
            object_id=conversation_id,
        )
        if already:
            log.info("reflect_turn already completed for %s; skipping",
                     conversation_id)
            await session.commit()
            return

        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            raise ValueError(f"conversation {conversation_id!r} not found")
        if conversation.ended_at is None:
            raise ValueError(f"conversation {conversation_id!r} not yet ended; cannot reflect")

        involved_entry_ids = _collect_involved_entry_ids(conversation)
        entry_metadata = await _fetch_entry_metadata(session, involved_entry_ids)
        await session.commit()

    # --- 2. LLM call (outside DB transaction)
    payload_for_llm = {
        "conversation": {
            "user_message": conversation.user_message,
            "agent_response": conversation.agent_response,
            "tool_calls": conversation.tool_calls or [],
            "llm_calls": conversation.llm_calls or [],
        },
        "involved_entries": entry_metadata,
    }
    user_text = (
        "Below is one finished conversation along with the current metadata of "
        "the file_entries the agent touched. Decide what to remember.\n\n"
        f"<conversation_and_context>\n{json.dumps(payload_for_llm, ensure_ascii=False)}\n</conversation_and_context>"
    )

    client = get_chat_client("reflect")
    resp = await client.complete(ChatRequest(
        system=REFLECT_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        json_schema=REFLECT_SCHEMA,
        temperature=0.3,
    ))
    if resp.parsed_json is None:
        raise ValueError("reflect_turn: model did not return parseable JSON")

    data = resp.parsed_json

    # --- 3. persist all writes in one transaction
    async with session_scope() as session:
        await _persist_reflection(session, conversation_id=conversation_id, data=data)
        await session.commit()


def _collect_involved_entry_ids(conv: Conversation) -> list[str]:
    """Pull entry_ids out of tool_calls payloads.

    Convention: tool_calls is a JSON array of `{name, arguments, result, ...}`
    where `arguments` and `result` are dicts. Any string value at any depth
    that looks like a uuid7 we accept as a candidate (cheap; the metadata
    fetch will quietly drop unknowns).
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for call in (conv.tool_calls or []):
        for blob in (call.get("arguments"), call.get("result")):
            for v in _walk_strings(blob):
                if _looks_like_id(v) and v not in seen_set:
                    seen_set.add(v)
                    seen.append(v)
                    if len(seen) >= ENTRY_LIMIT:
                        return seen
    return seen


def _walk_strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)


def _looks_like_id(s: str) -> bool:
    return len(s) == 36 and s.count("-") == 4


async def _fetch_entry_metadata(session, entry_ids: list[str]) -> list[dict[str, Any]]:
    if not entry_ids:
        return []
    rows = (
        await session.execute(
            select(FileEntry).where(FileEntry.id.in_(entry_ids))
        )
    ).scalars().all()
    out: list[dict[str, Any]] = []
    for e in rows:
        file_row = await session.get(File, e.file_id)
        tag_rows = (
            await session.execute(
                select(Tag.name, Tag.facet)
                .join(EntryTag, Tag.id == EntryTag.tag_id)
                .where(EntryTag.entry_id == e.id)
            )
        ).all()
        out.append({
            "entry_id": e.id,
            "display_name": e.display_name,
            "lifecycle": e.lifecycle,
            "extra": e.extra,
            "file": {
                "kind": file_row.kind if file_row else None,
                "summary": file_row.summary if file_row else None,
            },
            "tags": [{"name": n, "facet": f} for n, f in tag_rows],
        })
    return out


async def _persist_reflection(
    session,
    *,
    conversation_id: str,
    data: dict[str, Any],
) -> None:
    now = _utcnow()
    counts = {
        "journal_entries": 0,
        "relations_inserted": 0,
        "relations_incremented": 0,
        "tag_additions": 0,
        "entry_extra_updates": 0,
        "catalog_extra_updates": 0,
        "view_extra_updates": 0,
        "tags_created": 0,
    }

    # --- journal ----------------------------------------------------------
    for j in data.get("journal_entries") or []:
        session.add(Journal(
            id=new_id(),
            conversation_id=conversation_id,
            note=j["note"],
            entry_ids=list(j.get("entry_ids") or []),
            tags=list(j.get("tags") or []),
            source_kind="reflect_turn",
            created_at=now,
        ))
        counts["journal_entries"] += 1

    # --- entry_relations --------------------------------------------------
    for rel in data.get("entry_relations") or []:
        a_raw = rel["entry_a_id"]
        b_raw = rel["entry_b_id"]
        if a_raw == b_raw:
            log.warning("reflect_turn: skip self-relation %s", a_raw)
            continue
        a, b = sorted((a_raw, b_raw))
        existing = (
            await session.execute(
                select(EntryRelation).where(
                    EntryRelation.entry_a_id == a,
                    EntryRelation.entry_b_id == b,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            # only insert if both entries exist (FK)
            if not await _entries_exist(session, [a, b]):
                log.warning("reflect_turn: skip relation referencing missing entries %s/%s", a, b)
                continue
            session.add(EntryRelation(
                id=new_id(),
                entry_a_id=a,
                entry_b_id=b,
                note=rel["note"],
                source_kind="reflect",
                last_observed_at=now,
                observation_count=1,
                created_at=now,
            ))
            counts["relations_inserted"] += 1
        else:
            existing.observation_count = (existing.observation_count or 0) + 1
            existing.last_observed_at = now
            new_note = rel.get("note") or ""
            if new_note and new_note not in (existing.note or ""):
                existing.note = (existing.note or "") + "\n---\n" + new_note
            counts["relations_incremented"] += 1

    # --- entry_tag_additions ---------------------------------------------
    for ta in data.get("entry_tag_additions") or []:
        entry_id = ta["entry_id"]
        if not await _entries_exist(session, [entry_id]):
            continue
        tag_id, created = await _resolve_or_create_tag(
            session, name=ta["name"], facet=ta["facet"], now=now
        )
        if created:
            counts["tags_created"] += 1
        existing = (
            await session.execute(
                select(EntryTag).where(
                    EntryTag.entry_id == entry_id, EntryTag.tag_id == tag_id
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(EntryTag(
                entry_id=entry_id,
                tag_id=tag_id,
                source="reflect",
                created_at=now,
            ))
            counts["tag_additions"] += 1

    # --- entry_extra_updates ---------------------------------------------
    for u in data.get("entry_extra_updates") or []:
        entry_id = u["entry_id"]
        extra = u.get("extra") or None
        result = await session.execute(
            update(FileEntry)
            .where(FileEntry.id == entry_id, FileEntry.deleted_at.is_(None))
            .values(extra=extra, updated_at=now)
        )
        if result.rowcount:
            counts["entry_extra_updates"] += 1

    # --- catalog_extra_updates -------------------------------------------
    for u in data.get("catalog_extra_updates") or []:
        result = await session.execute(
            update(Catalog)
            .where(Catalog.id == u["catalog_id"], Catalog.deleted_at.is_(None))
            .values(extra=(u.get("extra") or None), updated_at=now)
        )
        if result.rowcount:
            counts["catalog_extra_updates"] += 1

    # --- view_extra_updates ----------------------------------------------
    for u in data.get("view_extra_updates") or []:
        result = await session.execute(
            update(View)
            .where(View.id == u["view_id"])
            .values(extra=(u.get("extra") or None), updated_at=now)
        )
        if result.rowcount:
            counts["view_extra_updates"] += 1

    await record_outcome(
        session,
        task_kind="reflect_turn",
        object_kind="conversation",
        object_id=conversation_id,
        outcome="applied",
        detail=counts,
    )


async def _entries_exist(session, entry_ids: list[str]) -> bool:
    if not entry_ids:
        return False
    rows = (
        await session.execute(
            select(FileEntry.id).where(FileEntry.id.in_(entry_ids))
        )
    ).scalars().all()
    return len(rows) == len(set(entry_ids))


async def _resolve_or_create_tag(
    session, *, name: str, facet: str, now: datetime
) -> tuple[str, bool]:
    """Return (tag_id, created). Follows alias_of chain if present."""
    existing = (
        await session.execute(
            select(Tag).where(Tag.name == name, Tag.facet == facet)
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.alias_of:
            return existing.alias_of, False
        existing.doc_count = (existing.doc_count or 0) + 1
        existing.last_used_at = now
        return existing.id, False

    tag = Tag(
        id=new_id(),
        name=name,
        facet=facet,
        alias_of=None,
        doc_count=1,
        last_used_at=now,
        created_at=now,
        updated_at=now,
    )
    session.add(tag)
    await session.flush()
    await write_event(
        session,
        kind="tag_created",
        payload={"tag_id": tag.id, "name": name, "facet": facet, "source": "reflect"},
    )
    return tag.id, True
