"""reflect_turn handler — single responsibility: write one journal row.

Identity: [🔍 investigator]. Reads one finished turn (user message +
agent response + tool_calls) and asks the `reflect` LLM profile to
produce a structured field-log entry (question + answer + entry_ids
+ tags) that the future planner can recall when a similar question
returns.

Scope (intentionally narrow as of 2026-05-24):
  - The ONLY write this handler performs is INSERT INTO journal.
  - Per-conversation increments to entry_relations / entry_tags / *_extra
    were removed — those signals are weaker per-conversation than the
    cross-corpus miners (`mine_*`, `enrich_tags`, `refresh_entry_extra`,
    `propose_views`) that already cover the same ground.
  - Cross-session synthesis (the "big summary" tier) lives in
    `summarize_session`, which reads many reflect_turn rows and writes
    `source_kind='insight'` journal rows — see [[journal-tiers]].

Inputs:
  payload = {"conversation_id": "..."}

Flow:
  1. Idempotence: short-circuit on existing task_outcomes row.
  2. Pull the conversation; require it to be ended.
  3. Resolve involved entry_ids from tool_calls payload (read trail).
  4. Call the `reflect` LLM profile with strict JSON schema.
  5. INSERT 0..1 journal rows; record_outcome.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from marginalia.db.models import (
    Conversation,
    File,
    Journal,
)
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    get_chat_client,
)
from marginalia.llm.tagged_response import parse_tagged
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories.task_outcomes import has_outcome, record_outcome
from marginalia.tasks.kinds import task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

KIND_REFLECT_TURN = "reflect_turn"

ENTRY_LIMIT = 30  # cap how many entries we feed the model context for


REFLECT_SYSTEM = """You are Marginalia's reflection investigator.

You read ONE finished turn between the user and the Marginalia agent
(user message + agent's full response + tool_calls + llm_calls), plus
the current metadata of file_entries the agent touched. You write a
faithful, compressed record of THIS turn — a "field log entry" the
future planner can recall when a similar question comes back.

For each turn, decide:

1. If the turn has NOTHING to do with the corpus — pure small talk,
   weather, "what can you do", system meta — leave the <entry> block
   empty. The framework will skip the write.

2. Otherwise, fill the <entry> block with these fields:

   - question: the user's question in their own framing, as concise as
     possible while still being a real question (not a topic label).
     Length follows content — short for simple lookups, longer when the
     ask was multi-part.
   - answer:   what the investigation actually concluded. Strip the
     prose layer — greetings, formatting, restating the question — and
     keep only epistemic content: findings, key names, numbers,
     meaningful turns of the investigation. Be as concise as possible
     while preserving every distinct finding; length follows content,
     do not pad. If the agent said "I don't know" or "no match", say
     so plainly — that null result is itself worth recalling.
   - entry_ids: every file_entry the agent read or cited in this turn
     that was actually relevant to the answer. Skip entries the agent
     looked at but discarded.
   - tags: topical tags useful for later recall. Subject of the
     question, not housekeeping tags.

A turn touching the corpus ALWAYS produces one entry, even if the
answer was "nothing found" — that null result is itself worth recalling.
Only leave the block empty for turns that never engaged the corpus.

Output format — exactly one block:

  <entry>
  question: one-line question
  answer: free-form text; may span multiple lines
  entry_ids: id1, id2
  tags: tag1, tag2
  </entry>

Each field starts with its label on its own line. The `answer:` field
may run across multiple lines; the next labeled field (`entry_ids:`,
`tags:`) ends it. Leave the entire block EMPTY (or omit field values)
to skip the write. Do NOT wrap in JSON or add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
REFLECT_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_REFLECT_TURN)
async def handle_reflect_turn(payload: Mapping[str, Any]) -> None:
    conversation_id = payload.get("conversation_id")
    if not conversation_id:
        raise ValueError("reflect_turn payload missing conversation_id")

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
            raise ValueError(
                f"conversation {conversation_id!r} not yet ended; cannot reflect"
            )

        involved_entry_ids = _collect_involved_entry_ids(conversation)
        entry_metadata = await _fetch_entry_metadata(session, involved_entry_ids)
        await session.commit()

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
        "Below is one finished turn along with the current metadata of "
        "the file_entries the agent touched. Produce a structured field-"
        "log entry (question + answer + entry_ids + tags) for the journal, "
        "or skip if the turn never engaged the corpus.\n\n"
        f"<conversation_and_context>\n"
        f"{json.dumps(payload_for_llm, ensure_ascii=False)}\n"
        "</conversation_and_context>"
    )

    client = get_chat_client("reflect")
    resp = await client.complete(ChatRequest(
        system=REFLECT_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=2048,
        temperature=0.3,
    ))
    tagged = parse_tagged(resp.text or "")
    entry = _parse_entry_block(tagged.get("entry", ""))
    data: dict[str, Any] = {
        "journal_entries": [entry] if entry is not None else [],
    }

    async with session_scope() as session:
        await _persist_reflection(
            session, conversation_id=conversation_id, data=data,
        )
        await session.commit()


def _parse_entry_block(block: str) -> dict[str, Any] | None:
    """Parse the <entry> block into one journal-entry dict, or None if empty.

    The `answer:` field may span multiple lines; it ends when the next
    labeled field (`entry_ids:` or `tags:`) starts.
    """
    fields: dict[str, str] = {"question": "", "answer": "", "entry_ids": "", "tags": ""}
    current_key: str | None = None
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        # Detect a field-label line.
        matched_key: str | None = None
        for key in ("question:", "answer:", "entry_ids:", "tags:"):
            if stripped.startswith(key):
                matched_key = key.rstrip(":")
                value = stripped[len(key):].strip()
                fields[matched_key] = value
                current_key = matched_key
                break
        if matched_key is not None:
            continue
        # Continuation: append to the current field (only really useful for
        # `answer`, but harmless elsewhere).
        if current_key and stripped:
            sep = "\n" if current_key == "answer" else " "
            fields[current_key] = (
                fields[current_key] + sep + stripped
                if fields[current_key]
                else stripped
            )

    question = fields["question"].strip()
    answer = fields["answer"].strip()
    if not question and not answer:
        return None
    entry_ids = [
        t.strip() for t in fields["entry_ids"].split(",") if t.strip()
    ]
    tags = [t.strip() for t in fields["tags"].split(",") if t.strip()]
    return {
        "question": question,
        "answer": answer,
        "entry_ids": entry_ids,
        "tags": tags,
    }


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
    rows = await entries_repo.list_by_ids_any(session, entry_ids)
    out: list[dict[str, Any]] = []
    for e in rows:
        file_row = await session.get(File, e.file_id)
        tag_rows = await entry_tags_repo.list_name_facet_for_entry(session, e.id)
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
    journal_count = 0

    for j in data.get("journal_entries") or []:
        question = (j.get("question") or "").strip()
        answer = (j.get("answer") or "").strip()
        if not question and not answer:
            continue
        note = f"Q: {question}\nA: {answer}"
        session.add(Journal(
            id=new_id(),
            conversation_id=conversation_id,
            note=note,
            entry_ids=list(j.get("entry_ids") or []),
            tags=list(j.get("tags") or []),
            source_kind="reflect_turn",
            created_at=now,
        ))
        journal_count += 1

    await record_outcome(
        session,
        task_kind="reflect_turn",
        object_kind="conversation",
        object_id=conversation_id,
        outcome="applied" if journal_count else "noop",
        detail={"journal_entries": journal_count},
    )
