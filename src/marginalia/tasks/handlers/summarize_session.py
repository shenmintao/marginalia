"""summarize_session — distill per-turn reflect_turn rows into cross-session insight.

This is the "big summary" tier of the journal — see [[journal-tiers]]:
  reflect_turn rows are the per-turn bullets a notebook fills with
  during one session; this handler is the "end-of-day write-up" that
  reads N bullets and produces 1..M `source_kind='insight'` rows the
  agent will read at the START of FUTURE sessions.

Trigger:
  - Periodic — periodic_tick enqueues one `summarize_session` task per
    eligible session (≥MIN_TURNS turns, no recent insight). dedup_key
    encodes the session_id, so periodic_tick can fire frequently with
    no risk of duplicate work.
  - Future: explicit `/clear` will enqueue with the same dedup_key,
    making the user's "this session is done" signal immediate.

Inputs:
  payload = {"session_id": "..."}

Flow:
  1. Idempotence: if a recent (< MIN_INTERVAL) task_outcomes row exists
     for this (task_kind='summarize_session', object_id=session_id),
     skip — likely a duplicate enqueue.
  2. Pull the session, all its conversations, all reflect_turn journal
     rows whose conversation_id belongs to this session.
  3. If reflect_turn count < MIN_TURNS, skip (record noop).
  4. Pull the involved entry_ids' metadata (display_name + summary).
  5. Call the `reflect` LLM profile with strict JSON schema asking for
     1..MAX_INSIGHTS distilled `note + entry_ids + tags` items, plus
     `superseded` — a list of older insight ids this run replaces.
  6. INSERT new insight rows (source_kind='insight'). For each entry in
     `superseded`, set the older row's `superseded_by_id = new_row.id`
     so the evolution chain is preserved.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from marginalia.db.models import (
    File,
    Journal,
    Session,
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
from marginalia.repositories import journal as journal_repo
from marginalia.repositories import sessions as sessions_repo
from marginalia.repositories.task_outcomes import has_outcome, record_outcome
from marginalia.tasks.kinds import KIND_SUMMARIZE_SESSION, task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

MIN_TURNS = 3
MIN_INTERVAL = timedelta(hours=24)
MAX_INSIGHTS = 5
MAX_REFLECT_ROWS = 60
MAX_ENTRIES_FOR_LLM = 30


SUMMARIZE_SYSTEM = """You are Marginalia's session summarizer.

You read ONE finished session's full reflect_turn journal — the per-turn
bullets the investigator wrote during the session — and distill them into
a small number of cross-session "insights": durable notes that future
sessions should see when starting fresh.

Two kinds of insights are useful:
  - Conclusions about the corpus or the user's questions ("the user
    favors Raft over Paxos in their reading list").
  - Operational lessons ("for `vault/papers/`, ingestion repeatedly
    misclassified language=zh — worth a tag fix").

Rules:
  - Be SELECTIVE. 0..MAX insights per session. If nothing is durable,
    leave the <insights> block empty.
  - Each insight stands alone. Don't reference "this session" — write as
    if a future session is reading it cold.
  - Tag liberally so search_journal can find them later. Use entry_ids
    only for genuinely entry-specific insights.
  - If an OLDER insight (provided in <prior_insights>) is now obsolete
    or refined, list its id in <superseded>; the framework will chain
    them. Do NOT include the older insight's text — the new one stands
    alone.

Output format — exactly two blocks:

  <insights>
  - note: free-form one or two sentences
    entry_ids: id1, id2
    tags: tag1, tag2
  - note: another insight
    entry_ids:
    tags: tag3
  </insights>

  <superseded>
  old_insight_id_1
  old_insight_id_2
  </superseded>

Each insight starts with `- note:` on its own line, followed by the
`entry_ids:` and `tags:` lines (comma-separated, may be empty). The
<superseded> block lists one prior_insight id per line; leave empty
if none. Do NOT wrap in JSON or add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
SUMMARIZE_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_insights_block(block: str) -> list[dict[str, Any]]:
    """Parse the <insights> block into a list of {note, entry_ids, tags}.

    Format (one item per `- note:`):
        - note: free-form text
          entry_ids: id1, id2
          tags: tag1, tag2
    """
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in block.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith("- note:"):
            if current is not None:
                items.append(current)
            current = {
                "note": stripped[len("- note:"):].strip(),
                "entry_ids": [],
                "tags": [],
            }
            continue
        if current is None:
            continue
        if stripped.startswith("entry_ids:"):
            csv = stripped[len("entry_ids:"):].strip()
            current["entry_ids"] = [
                t.strip() for t in csv.split(",") if t.strip()
            ]
        elif stripped.startswith("tags:"):
            csv = stripped[len("tags:"):].strip()
            current["tags"] = [
                t.strip() for t in csv.split(",") if t.strip()
            ]
        else:
            # Continuation of the note line.
            current["note"] = (current["note"] + " " + stripped).strip()
    if current is not None:
        items.append(current)
    return items


@task_handler(KIND_SUMMARIZE_SESSION)
async def handle_summarize_session(payload: Mapping[str, Any]) -> None:
    session_id = payload.get("session_id")
    if not session_id:
        raise ValueError("summarize_session payload missing session_id")

    async with session_scope() as session:
        if await _recently_summarized(session, session_id):
            log.info(
                "summarize_session: %s summarized recently; skipping",
                session_id,
            )
            await session.commit()
            return

        session_row = await session.get(Session, session_id)
        if session_row is None:
            raise ValueError(f"session {session_id!r} not found")

        reflect_rows = await _fetch_reflect_rows(session, session_id)
        if len(reflect_rows) < MIN_TURNS:
            await record_outcome(
                session,
                task_kind=KIND_SUMMARIZE_SESSION,
                object_kind="session",
                object_id=session_id,
                outcome="noop",
                detail={
                    "reason": "below_min_turns",
                    "reflect_turn_rows": len(reflect_rows),
                    "min_turns": MIN_TURNS,
                },
            )
            await session.commit()
            return

        involved_entry_ids = _collect_entry_ids(reflect_rows)
        entry_metadata = await _fetch_entry_metadata(
            session, involved_entry_ids,
        )
        prior_insights = await _fetch_prior_insights(session, session_id)
        last_conversation_id = await _last_conversation_id(session, session_id)
        await session.commit()

    if last_conversation_id is None:
        log.warning(
            "summarize_session: session %s has reflect rows but no "
            "conversation; aborting", session_id,
        )
        return

    payload_for_llm = {
        "reflect_journal": [
            {
                "id": j["id"],
                "note": j["note"],
                "entry_ids": j["entry_ids"],
                "tags": j["tags"],
                "created_at": j["created_at"],
            }
            for j in reflect_rows
        ],
        "involved_entries": entry_metadata,
        "prior_insights": prior_insights,
    }
    user_text = (
        "Distill the session below into durable cross-session insights "
        f"(0..{MAX_INSIGHTS} items). The reflect_journal is the per-turn "
        "bullets the investigator wrote during the session.\n\n"
        f"<session>\n{json.dumps(payload_for_llm, ensure_ascii=False)}\n</session>"
    )

    client = get_chat_client("reflect")
    resp = await client.complete(ChatRequest(
        system=SUMMARIZE_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        temperature=0.3,
    ))
    tagged = parse_tagged(resp.text or "")
    raw_insights = _parse_insights_block(tagged.get("insights", ""))[:MAX_INSIGHTS]
    raw_superseded = [
        line.strip()
        for line in tagged.get("superseded", "").splitlines()
        if line.strip()
    ]

    async with session_scope() as session:
        await _persist_insights(
            session,
            session_id=session_id,
            anchor_conversation_id=last_conversation_id,
            insights=raw_insights,
            superseded_ids=raw_superseded,
            summarized_journal_ids=[r["id"] for r in reflect_rows],
        )
        await session.commit()


async def _recently_summarized(session, session_id: str) -> bool:
    cutoff = _utcnow() - MIN_INTERVAL
    return await has_outcome(
        session,
        task_kind=KIND_SUMMARIZE_SESSION,
        object_kind="session",
        object_id=session_id,
        since=cutoff,
    )


async def _fetch_reflect_rows(session, session_id: str) -> list[dict[str, Any]]:
    rows = await journal_repo.list_reflect_rows_for_session(
        session, session_id, limit=MAX_REFLECT_ROWS,
    )
    return [
        {
            "id": jid,
            "note": note,
            "entry_ids": list(eids or []),
            "tags": list(tags or []),
            "created_at": created.isoformat() if created else None,
            "conversation_id": conv_id,
        }
        for (jid, note, eids, tags, created, conv_id) in rows
    ]


def _collect_entry_ids(reflect_rows: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for r in reflect_rows:
        for eid in r["entry_ids"]:
            if eid in seen_set:
                continue
            seen_set.add(eid)
            seen.append(eid)
            if len(seen) >= MAX_ENTRIES_FOR_LLM:
                return seen
    return seen


async def _fetch_entry_metadata(
    session, entry_ids: list[str],
) -> list[dict[str, Any]]:
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
            "file_summary": file_row.summary if file_row else None,
            "tags": [{"name": n, "facet": f} for n, f in tag_rows],
        })
    return out


async def _fetch_prior_insights(
    session, session_id: str,
) -> list[dict[str, Any]]:
    """Fetch active insights from OTHER sessions involving any entry in
    this session — gives the LLM the chain it might be replacing."""
    own_conv_ids = await sessions_repo.list_conversation_ids(session, session_id)
    if not own_conv_ids:
        return []
    own_entries: set[str] = set()
    for eids in await journal_repo.list_entry_id_lists_for_conversations(
        session, own_conv_ids,
    ):
        own_entries.update(eids)
    if not own_entries:
        return []
    rows = await journal_repo.list_active_insights_recent(session, limit=20)
    out: list[dict[str, Any]] = []
    for jid, note, eids, tags, created in rows:
        eid_set = set(eids or [])
        if eid_set and not (eid_set & own_entries):
            continue
        out.append({
            "id": jid,
            "note": note,
            "entry_ids": list(eids or []),
            "tags": list(tags or []),
            "created_at": created.isoformat() if created else None,
        })
    return out


async def _last_conversation_id(session, session_id: str) -> str | None:
    return await sessions_repo.last_conversation_id(session, session_id)


async def _persist_insights(
    session,
    *,
    session_id: str,
    anchor_conversation_id: str,
    insights: list[Mapping[str, Any]],
    superseded_ids: list[str],
    summarized_journal_ids: list[str],
) -> None:
    now = _utcnow()
    inserted_ids: list[str] = []
    for ins in insights:
        note = (ins.get("note") or "").strip()
        if not note:
            continue
        new_journal = Journal(
            id=new_id(),
            conversation_id=anchor_conversation_id,
            note=note,
            entry_ids=list(ins.get("entry_ids") or []),
            tags=list(ins.get("tags") or []),
            source_kind="insight",
            superseded_by_id=None,
            summarized_journal_ids=list(summarized_journal_ids),
            created_at=now,
        )
        session.add(new_journal)
        inserted_ids.append(new_journal.id)

    if inserted_ids:
        # Flush so the new rows are visible to the supersedure UPDATE's FK.
        await session.flush()

    chain_count = 0
    if superseded_ids and inserted_ids:
        chain_to = inserted_ids[0]
        valid_olds = await journal_repo.filter_active_insight_ids(
            session, superseded_ids,
        )
        if valid_olds:
            await journal_repo.mark_superseded(
                session, valid_olds, by_id=chain_to,
            )
            chain_count = len(valid_olds)

    await record_outcome(
        session,
        task_kind=KIND_SUMMARIZE_SESSION,
        object_kind="session",
        object_id=session_id,
        outcome="applied" if inserted_ids else "noop",
        detail={
            "insights_inserted": len(inserted_ids),
            "superseded_chained": chain_count,
        },
    )
