"""mine_tag_overlap — structural entry-relation mining via tag Jaccard.

Purpose:
  Two entries that share many tags are likely related, even if no journal
  has explicitly co-cited them yet. This miner gives the discovery layer
  a useful signal even on a fresh library where mine_session_cooccurrence
  has no data — and complements cooccurrence on a mature library by
  surfacing structural neighbours the agent hasn't gotten around to
  thinking about together yet.

Algorithm:
  1. Pull every (entry_id, tag_id) pair where the entry is live.
  2. Group by entry → set of tag_ids; then by tag → set of entries
     wearing it.
  3. For every tag with >= 2 entries (and <= MAX_TAG_FANOUT to skip
     "default" / "untagged" / popular catch-all tags), emit candidate
     pairs from that tag's entry set.
  4. Score each candidate pair by Jaccard: |A ∩ B| / |A ∪ B| over their
     tag sets. Higher Jaccard = more structurally aligned.
  5. Filter pairs with Jaccard >= MIN_JACCARD and >= MIN_SHARED_TAGS to
     drop noise.
  6. Cap at MAX_NEW_RELATIONS_PER_RUN and emit, ordered by Jaccard desc.
  7. Upsert into entry_relations with source_kind='mine_tag_overlap':
     existing pair (any source_kind) → bump observation_count by
     ceil(jaccard * 10); else INSERT.

Writes:
  - entry_relations (INSERT new + UPDATE existing observation_count)
  - audit_events: 'relation_mined' per row
  - task_outcomes: per-pair detail + global summary

Does NOT write to: tags / entry_tags / file_entries / catalogs / journal /
files / views.
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import select

from marginalia.db.models import EntryTag, FileEntry
from marginalia.db.session import session_scope
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.handlers._mining_helpers import upsert_relation_pair
log = logging.getLogger(__name__)

MIN_JACCARD = 0.30
MIN_SHARED_TAGS = 2
# Tags worn by more than this many entries are too generic to be a
# useful similarity signal — skip them when seeding candidate pairs.
# (Pairs whose Jaccard is high overall will still surface from other
# shared tags they have.)
MAX_TAG_FANOUT = 40
MAX_NEW_RELATIONS_PER_RUN = 50
SOURCE_KIND = "mine_tag_overlap"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def handle_mine_tag_overlap(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    min_jaccard = float(payload.get("min_jaccard") or MIN_JACCARD)
    min_shared = int(payload.get("min_shared_tags") or MIN_SHARED_TAGS)
    max_fanout = int(payload.get("max_tag_fanout") or MAX_TAG_FANOUT)
    cap = int(payload.get("cap") or MAX_NEW_RELATIONS_PER_RUN)

    new_relations = 0
    incremented = 0
    candidates_considered = 0
    pairs_above_threshold = 0
    skipped_dead_entry = 0

    async with session_scope() as session:
        # 1. Pull all (entry, tag) edges where entry is live.
        rows = (
            await session.execute(
                select(EntryTag.entry_id, EntryTag.tag_id)
                .join(FileEntry, FileEntry.id == EntryTag.entry_id)
                .where(FileEntry.deleted_at.is_(None))
            )
        ).all()

        entry_tags: dict[str, set[str]] = defaultdict(set)
        tag_entries: dict[str, set[str]] = defaultdict(set)
        for entry_id, tag_id in rows:
            entry_tags[entry_id].add(tag_id)
            tag_entries[tag_id].add(entry_id)

        # 2. Build candidate pairs from each tag's entry set, skipping
        #    over-popular tags. Use a set so a pair seeded from multiple
        #    tags is only scored once.
        candidate_pairs: set[tuple[str, str]] = set()
        for tag_id, entries in tag_entries.items():
            if len(entries) < 2 or len(entries) > max_fanout:
                continue
            sorted_entries = sorted(entries)
            for i in range(len(sorted_entries)):
                for j in range(i + 1, len(sorted_entries)):
                    candidate_pairs.add(
                        (sorted_entries[i], sorted_entries[j])
                    )
        candidates_considered = len(candidate_pairs)

        # 3. Score each pair by Jaccard.
        scored: list[tuple[str, str, float, int]] = []
        for a, b in candidate_pairs:
            ta, tb = entry_tags[a], entry_tags[b]
            shared = ta & tb
            if len(shared) < min_shared:
                continue
            union = ta | tb
            if not union:
                continue
            jaccard = len(shared) / len(union)
            if jaccard < min_jaccard:
                continue
            scored.append((a, b, jaccard, len(shared)))

        scored.sort(key=lambda r: r[2], reverse=True)
        pairs_above_threshold = len(scored)

        # 4. Validate live entries (rare: should already be live since
        #    we filtered EntryTag join; defence-in-depth against races).
        all_ids: set[str] = set()
        for a, b, _, _ in scored:
            all_ids.add(a)
            all_ids.add(b)
        live_ids = await _live_entry_ids(session, all_ids) if all_ids else set()

        for a, b, jaccard, shared_count in scored:
            if new_relations >= cap:
                break
            if a not in live_ids or b not in live_ids:
                skipped_dead_entry += 1
                continue
            # observation_count is integer; map jaccard to a small
            # weight. ceil(jaccard * 10) lands in 1-10 range, comparable
            # to cooccurrence counts so the random-walk graph isn't
            # lopsided by signal source.
            obs_add = max(1, math.ceil(jaccard * 10))
            note = (
                f"Tag overlap: {shared_count} shared tags, "
                f"Jaccard {jaccard:.2f}."
            )
            _, action = await upsert_relation_pair(
                session,
                entry_a_id=a, entry_b_id=b,
                observation_add=obs_add,
                source_kind=SOURCE_KIND,
                note=note,
                now=now,
                audit_extra={
                    "jaccard": round(jaccard, 3),
                    "shared_tags": shared_count,
                },
            )
            if action == "incremented":
                incremented += 1
            else:
                new_relations += 1

        await record_outcome(
            session,
            task_kind="mine_tag_overlap",
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if (new_relations or incremented) else "noop",
            detail={
                "min_jaccard": min_jaccard,
                "min_shared_tags": min_shared,
                "max_tag_fanout": max_fanout,
                "cap": cap,
                "candidates_considered": candidates_considered,
                "pairs_above_threshold": pairs_above_threshold,
                "new_relations": new_relations,
                "incremented_relations": incremented,
                "skipped_dead_entry": skipped_dead_entry,
            },
        )
        await session.commit()

    log.info(
        "mine_tag_overlap: candidates=%d above_threshold=%d "
        "new=%d incremented=%d",
        candidates_considered, pairs_above_threshold,
        new_relations, incremented,
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
