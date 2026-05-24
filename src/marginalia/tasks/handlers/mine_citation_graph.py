"""mine_citation_graph — agent-citation signal for entry_relations.

When the agent answers a question it lists citation footnotes attached
to claims; entries cited together within a single turn are likely
content-related. This miner aggregates that signal into entry_relations
so the discovery layer (random-walk find_related) sees it as another
edge type alongside session-cooccurrence and tag-overlap.

Distinct from mine_session_cooccurrence in granularity:
  - cooccurrence:  X and Y appeared together in the same JOURNAL note
                   (whole conversation, written by reflect_turn)
  - citation:      X and Y appeared together in the same TURN's citations
                   (within a single answer, possibly several per session)

A single conversation can produce many citation co-occurrences but only
one journal entry. Citation is the finer signal; cooccurrence is the
broader one. Both feed entry_relations and balance each other on the
random-walk graph.

Algorithm:
  1. Pull recent assistant messages (default 30 days) whose `citations`
     field is non-empty.
  2. For each message extract the unique entry_ids cited; emit pairs
     (a, b) for every two entries cited in the same turn.
  3. Sum across all turns. Filter pairs with count >= MIN_CITATIONS.
  4. Filter pairs where either entry is soft-deleted.
  5. Cap and upsert into entry_relations as source_kind='mine_citation_graph'.

Writes:
  - entry_relations (INSERT new + UPDATE existing observation_count)
  - audit_events: 'relation_mined' per row
  - task_outcomes: per-pair detail + global summary
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from marginalia.db.models import Conversation, FileEntry
from marginalia.db.session import session_scope
from marginalia.services.exports import parse_citations
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.handlers._mining_helpers import upsert_relation_pair
log = logging.getLogger(__name__)

CITATION_WINDOW_DAYS = 30
MIN_CITATIONS = 2
MAX_NEW_RELATIONS_PER_RUN = 50
SOURCE_KIND = "mine_citation_graph"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def handle_mine_citation_graph(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    cutoff = now - timedelta(
        days=int(payload.get("window_days") or CITATION_WINDOW_DAYS)
    )
    min_citations = int(payload.get("min_citations") or MIN_CITATIONS)
    cap = int(payload.get("cap") or MAX_NEW_RELATIONS_PER_RUN)

    new_relations = 0
    incremented = 0
    messages_scanned = 0
    pairs_above_threshold = 0
    skipped_dead_entry = 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Conversation.agent_response).where(
                    Conversation.agent_response.is_not(None),
                    Conversation.started_at >= cutoff,
                )
            )
        ).scalars().all()
        messages_scanned = len(rows)

        counter: Counter[tuple[str, str]] = Counter()
        for response in rows:
            entry_ids = _extract_entry_ids(response)
            if len(entry_ids) < 2:
                continue
            ids = sorted(entry_ids)
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    counter[(ids[i], ids[j])] += 1

        candidates = [
            (pair, n) for pair, n in counter.items() if n >= min_citations
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)
        pairs_above_threshold = len(candidates)

        all_ids: set[str] = set()
        for (a, b), _ in candidates:
            all_ids.add(a)
            all_ids.add(b)
        live_ids = await _live_entry_ids(session, all_ids) if all_ids else set()

        for (a, b), n in candidates:
            if new_relations >= cap:
                break
            if a not in live_ids or b not in live_ids:
                skipped_dead_entry += 1
                continue
            note = (
                f"Co-cited in {n} assistant turns from the last "
                f"{CITATION_WINDOW_DAYS} days."
            )
            _, action = await upsert_relation_pair(
                session,
                entry_a_id=a, entry_b_id=b,
                observation_add=n,
                source_kind=SOURCE_KIND,
                note=note,
                now=now,
            )
            if action == "incremented":
                incremented += 1
            else:
                new_relations += 1

        await record_outcome(
            session,
            task_kind="mine_citation_graph",
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if (new_relations or incremented) else "noop",
            detail={
                "window_days": int(payload.get("window_days") or CITATION_WINDOW_DAYS),
                "min_citations": min_citations,
                "cap": cap,
                "messages_scanned": messages_scanned,
                "pairs_above_threshold": pairs_above_threshold,
                "new_relations": new_relations,
                "incremented_relations": incremented,
                "skipped_dead_entry": skipped_dead_entry,
            },
        )
        await session.commit()

    log.info(
        "mine_citation_graph: messages=%d above_threshold=%d "
        "new=%d incremented=%d",
        messages_scanned, pairs_above_threshold,
        new_relations, incremented,
    )


def _extract_entry_ids(agent_response: str | None) -> set[str]:
    """Pull entry_ids out of footnote citations like
    `[^a]: entry_id=<uuid>[, section_id=...]` lines in an agent response.
    Reuses services.exports.parse_citations so the parsing rules stay
    consistent with conversation export."""
    if not agent_response:
        return set()
    return {c.entry_id for c in parse_citations(agent_response) if c.entry_id}


async def _live_entry_ids(session, ids: Iterable[str]) -> set[str]:
    rows = (
        await session.execute(
            select(FileEntry.id).where(
                FileEntry.id.in_(list(ids)),
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return set(rows)
