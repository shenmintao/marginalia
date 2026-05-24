"""mine_session_cooccurrence — usage-driven entry-relation mining.

Purpose:
  When two entries appear together in journal notes (the agent's reflective
  notebook) often enough, that's a use-driven signal that they belong
  together. We materialize this signal as new entry_relations rows.

Inputs:
  - Recent journal entries (default: last 30 days)
  - Each journal row has `entry_ids: list[str]` — the entries the
    investigator was thinking about together when writing that note

Algorithm:
  1. Pull all journal rows in the time window.
  2. For each row, compute every (a, b) pair from entry_ids (a < b lex).
  3. Count co-occurrences across rows. Filter pairs with count >= MIN_COOCCURRENCES.
  4. Filter pairs where either entry is soft-deleted.
  5. Cap at MAX_NEW_RELATIONS_PER_RUN new relations per invocation.
  6. For each surviving pair:
     - If entry_relation already exists (any source_kind):
         observation_count += co-occurrence count
         last_observed_at = now
     - Else INSERT new entry_relation with source_kind='mine_session_cooccurrence'

Writes:
  - entry_relations (INSERT new + UPDATE existing observation_count)
  - audit_events: 'relation_mined' per row
  - task_outcomes: per-pair detail + global summary

Does NOT write to: tags / entry_tags / file_entries / catalogs / journal /
files / views.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from marginalia.db.models import (
    FileEntry,
    Journal,
)
from marginalia.db.session import session_scope
from marginalia.tasks.handlers._mining_helpers import upsert_relation_pair
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
log = logging.getLogger(__name__)

JOURNAL_WINDOW_DAYS = 30
MIN_COOCCURRENCES = 2
MAX_NEW_RELATIONS_PER_RUN = 50
SOURCE_KIND = "mine_session_cooccurrence"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def handle_mine_session_cooccurrence(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    cutoff = now - timedelta(
        days=int(payload.get("window_days") or JOURNAL_WINDOW_DAYS)
    )
    min_cooccur = int(payload.get("min_cooccurrences") or MIN_COOCCURRENCES)
    cap = int(payload.get("cap") or MAX_NEW_RELATIONS_PER_RUN)

    new_relations = 0
    incremented = 0
    journals_scanned = 0
    pairs_above_threshold = 0
    skipped_dead_entry = 0

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Journal.entry_ids).where(Journal.created_at >= cutoff)
            )
        ).scalars().all()
        journals_scanned = len(rows)

        # 2. Count pairs across rows.
        counter: Counter[tuple[str, str]] = Counter()
        for entry_ids in rows:
            ids = sorted({str(e) for e in (entry_ids or []) if e})
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    counter[(ids[i], ids[j])] += 1

        # 3. Filter by threshold.
        candidates = [
            (pair, n) for pair, n in counter.items() if n >= min_cooccur
        ]
        candidates.sort(key=lambda x: x[1], reverse=True)  # most-cooccurred first
        pairs_above_threshold = len(candidates)

        # 4. Validate live entries: pull every involved id once.
        all_ids: set[str] = set()
        for (a, b), _ in candidates:
            all_ids.add(a)
            all_ids.add(b)
        live_ids = await _live_entry_ids(session, all_ids) if all_ids else set()

        for pair, n in candidates:
            if new_relations >= cap:
                break
            a, b = pair
            if a not in live_ids or b not in live_ids:
                skipped_dead_entry += 1
                continue
            note = (
                f"Co-occurred in {n} journal notes from the last "
                f"{JOURNAL_WINDOW_DAYS} days."
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
            task_kind="mine_session_cooccurrence",
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if (new_relations or incremented) else "noop",
            detail={
                "window_days": int(payload.get("window_days") or JOURNAL_WINDOW_DAYS),
                "min_cooccurrences": min_cooccur,
                "cap": cap,
                "journals_scanned": journals_scanned,
                "pairs_above_threshold": pairs_above_threshold,
                "new_relations": new_relations,
                "incremented_relations": incremented,
                "skipped_dead_entry": skipped_dead_entry,
            },
        )
        await session.commit()

    log.info(
        "mine_session_cooccurrence: journals=%d candidates=%d new=%d incremented=%d",
        journals_scanned, pairs_above_threshold, new_relations, incremented,
    )


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
