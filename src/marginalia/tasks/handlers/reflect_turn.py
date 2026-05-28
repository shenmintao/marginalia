"""reflect_turn handler — single responsibility: write one journal row.

Identity: [🔍 investigator]. Reads one finished turn (user message +
agent response + tool_calls) and asks the `reflect` LLM profile to
produce a structured field-log entry (question + answer + entry_ids
+ tags) that the future planner can recall when a similar question
returns.

**Prefix-cache reuse (2026-05-27):** The handler now replays the
session's conversation history using the same `build_resumed_messages`
+ execute-phase system prompt and cacheable snapshot messages that the agent
runtime uses for its execute phase. This gives the reflect LLM call
the identical prefix (system prompt + resumed history) that was
already sent to the chat model, so DeepSeek / OpenAI automatic
prefix caching kicks in — the reflect call only pays for the new
reflect-request message appended at the end, not the full history
prefix again.

Before this change, reflect_turn sent a fresh one-shot prompt with
the entire conversation payload serialized as JSON, producing zero
prefix overlap with the execute phase → 0% cache hit rate and
~35k input tokens per reflect call. After the change, the shared
prefix is cached → reflect input drops to ~3-5k (the new message
only).

Current layout keeps the execute-phase system prompt static and sends the
snapshot as a complete cacheable message prefix before resumed history. That
matches DeepSeek's complete-prefix cache-unit rule more closely than putting
the snapshot after phase-specific system text.

Scope (intentionally narrow):
  - The ONLY write this handler performs is INSERT INTO journal.
  - Cross-session synthesis lives in `summarize_session`.

Inputs:
  payload = {"conversation_id": "..."}

Flow:
  1. Idempotence: short-circuit on existing task_outcomes row.
  2. Pull the conversation; require it to be ended.
  3. Build resumed history (same prefix as execute phase).
  4. Append the current turn + reflect-request message.
  5. Call the `reflect` LLM profile with the execute-phase system
     prompt (prefix matches execute → cache hit).
  6. Parse the <entry> block; INSERT 0..1 journal rows; record_outcome.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from marginalia.agent.stable_context import (
    build_resumed_messages,
    build_stable_snapshot,
    build_snapshot_messages,
    render_phase_system_prompt,
)
from marginalia.db.models import (
    Conversation,
    Journal,
    Session as SessionRow,
)
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    get_chat_client,
)
from marginalia.llm.tagged_response import parse_tagged
from marginalia.repositories.task_outcomes import has_outcome, record_outcome
from marginalia.tasks.kinds import task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

KIND_REFLECT_TURN = "reflect_turn"

ENTRY_LIMIT = 30  # cap how many entries we feed the model context for

# Reflect instructions — embedded in the final user message rather than
# as a separate system prompt, so the system prompt stays identical to
# the execute phase (prefix-cache reuse).
REFLECT_INSTRUCTIONS = """\
Generate one investigation journal entry for future planning.

Decision:
1. If this turn is unrelated to the knowledge base, such as small talk,
   capability questions, or realtime external data, leave the <entry> block
   empty.
2. Otherwise, fill <entry>:

   - question: restate the user's question concisely, preserving meaning.
   - answer: record the actual investigation result only. Remove greetings,
     formatting, and repeated question text. If no evidence was found, write
     that directly; an empty result is worth remembering.
   - entry_ids: include only entry IDs the agent actually cited or read and
     that support the result. Skip items that were inspected and rejected.
   - tags: short topic tags useful for recall.

Always write an entry for knowledge-base-related turns, including "not found"
results. Only pure small talk should be empty.

Output exactly one block:

  <entry>
  question: one-line question
  answer: free text; may span lines
  entry_ids: id1, id2
  tags: tag1, tag2
  </entry>

`answer:` may span lines and ends when the next labeled field,
`entry_ids:` or `tags:`, starts. Leave the whole block empty, or omit field
values, to skip writing. Do not output JSON or fenced code."""


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

        # Build the stable snapshot using the session's started_at (same
        # frozen journal slice as execute phase → prefix matches).
        session_row = await session.get(SessionRow, conversation.session_id)
        if session_row is None:
            raise ValueError(f"session {conversation.session_id!r} not found")
        snapshot = await build_stable_snapshot(
            session, session_started_at=session_row.started_at,
        )
        await session.commit()

    # --- Build messages: reuse execute-phase prefix for cache hit ---
    system_prompt = render_phase_system_prompt(phase="execute")
    snapshot_messages = build_snapshot_messages(snapshot)
    resumed = await build_resumed_messages(
        conversation.session_id,
        current_conversation_id=conversation_id,
    )

    # Instead of replaying the current turn's full tool_calls (which
    # would add ~10k tokens of results the reflect model doesn't need
    # and wouldn't be in the cached prefix anyway), send a compact
    # summary of what happened in this turn. The cached prefix
    # (system_prompt + resumed_history) is byte-for-byte identical to
    # what the execute phase already sent → DeepSeek / OpenAI prefix
    # cache should hit on that portion.
    tool_names = [
        str(tc.get("name") or "tool")
        for tc in (conversation.tool_calls or [])
        if isinstance(tc, dict)
    ]
    tool_summary = ", ".join(tool_names) if tool_names else "(no tool calls)"
    reflect_tail = (
        f"Current turn summary:\n"
        f"- User question: {conversation.user_message}\n"
        f"- Tool calls: {tool_summary}\n"
        f"- Agent answer: {(conversation.agent_response or '(no answer)')[:500]}\n\n"
    )
    if involved_entry_ids:
        reflect_tail += (
            f"Entry IDs involved in this turn: "
            + ", ".join(involved_entry_ids)
            + "\n\n"
        )
    reflect_tail += REFLECT_INSTRUCTIONS

    reflect_messages = list(snapshot_messages) + list(resumed) + [
        ChatMessage(role="user", content=reflect_tail),
    ]

    client = get_chat_client("reflect")
    resp = await client.complete(ChatRequest(
        system=system_prompt,
        messages=reflect_messages,
        max_tokens=1024,
        cache_breakpoints=[0],
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
    that looks like a UUID we accept as a candidate (cheap; the metadata
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
