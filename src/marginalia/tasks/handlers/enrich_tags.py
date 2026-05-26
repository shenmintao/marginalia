"""enrich_tags — DESIGN.md §9.1 + §9.4 + §14.

LLM-driven gap filler. Walks active entries that haven't been enriched
recently and asks the LLM to pick additional tags FROM THE EXISTING
CANONICAL VOCABULARY ONLY (no new-tag creation in this task).

Eligibility:
  - file_entries.lifecycle ∈ ('active', 'manual_active')
  - file_entries.deleted_at IS NULL
  - the corresponding file is ingest_status='done' (we need a description)
  - no task_outcomes row for (task_kind='enrich_tags',
    object_kind='file_entry', object_id=<entry_id>) within ENRICH_INTERVAL.

Vocabulary feed:
  - canonical tags (alias_of IS NULL) grouped by facet, sorted by
    doc_count DESC, capped at TAG_VOCAB_TOP_PER_FACET.

Strict-vocabulary enforcement:
  - JSON schema lists tag IDs as an array; the LLM must pick from the
    supplied IDs (we cannot put thousands into an `enum`, so we accept
    free strings then verify against the supplied set on return).
  - Any returned id not in the supplied set is dropped.

Writes:
  - INSERT entry_tags(source='enrich_tags') for each accepted (entry, tag)
    pair, skipping if the row already exists OR if the same tag was already
    chosen earlier in the same call.
  - tags.doc_count is NOT incremented inline — normalize_tags recomputes it
    on its next run (every 6h vs enrich's 5d, so the lag is small).
  - record_outcome per entry (outcome='applied' if any tag added, 'noop'
    if nothing new) + one final summary row with object_kind='global'.

DESIGN.md §14.3 forbids reading audit_events for business logic (including
recency / idempotence). All scheduling decisions read task_outcomes.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from marginalia.db.models import EntryTag
from marginalia.db.session import session_scope
from marginalia.llm import ChatMessage, ChatRequest, TextBlock, get_chat_client
from marginalia.llm.tagged_response import parse_kv, parse_tagged
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
ENRICH_TASK_KIND = "enrich_tags"

log = logging.getLogger(__name__)

ENRICH_INTERVAL = timedelta(days=5)
ENRICH_BATCH = 5  # entries per LLM call
ENRICH_MAX_PER_RUN = 25  # cap per task invocation (5 batches × 5 entries)
TAG_VOCAB_TOP_PER_FACET = 60  # vocabulary feed size per facet


ENRICH_SYSTEM = """You are Marginalia's tag-gap filler.

You will see one or more file_entries (each with the parent file's summary +
description sketch + the entry's CURRENT tags) plus the canonical tag
vocabulary grouped by facet. Pick ADDITIONAL tag ids that should be attached
to each entry — only ones that are clearly justified by the entry's content
and that are NOT already attached.

Rules:
  - Only pick tag ids from the supplied vocabulary. NEVER coin new ones.
  - It is fine, and often correct, to return an empty list for an entry —
    do NOT pad. Conservative beats noisy.
  - Aim for 0–4 new tags per entry; never more than 6.
  - Do not duplicate the entry's existing tags.

Output format — one `<assignments>` block, one line per entry:

  <assignments>
  <entry_id>: tag_id1, tag_id2, tag_id3
  <entry_id>: (empty if no tags to add)
  </assignments>

Use the entry_id values verbatim from the input. Do NOT wrap in JSON or
add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
ENRICH_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def handle_enrich_tags(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    cutoff = now - ENRICH_INTERVAL

    candidates = await _select_candidates(cutoff=cutoff, limit=ENRICH_MAX_PER_RUN)
    if not candidates:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=ENRICH_TASK_KIND,
                object_kind=GLOBAL_OBJECT_KIND,
                object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"candidates": 0, "tags_added": 0},
            )
            await session.commit()
        return

    vocabulary = await _load_vocabulary()
    if not vocabulary:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=ENRICH_TASK_KIND,
                object_kind=GLOBAL_OBJECT_KIND,
                object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={
                    "candidates": len(candidates),
                    "tags_added": 0,
                    "skipped": "empty_vocabulary",
                },
            )
            await session.commit()
        return

    valid_tag_ids = {t["id"] for facet_tags in vocabulary.values() for t in facet_tags}

    total_added = 0
    entries_enriched = 0

    for batch_start in range(0, len(candidates), ENRICH_BATCH):
        batch = candidates[batch_start : batch_start + ENRICH_BATCH]
        assignments = await _ask_llm_for_batch(batch, vocabulary)
        added = await _apply_assignments(
            batch_entry_ids=[c["entry_id"] for c in batch],
            assignments=assignments,
            valid_tag_ids=valid_tag_ids,
            now=_utcnow(),
        )
        total_added += added
        entries_enriched += len(batch)

    async with session_scope() as session:
        await record_outcome(
            session,
            task_kind=ENRICH_TASK_KIND,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if total_added else "noop",
            detail={
                "candidates": len(candidates),
                "entries_enriched": entries_enriched,
                "tags_added": total_added,
            },
        )
        await session.commit()

    log.info(
        "enrich_tags: %d entries processed, %d tags added", entries_enriched, total_added
    )


async def _select_candidates(*, cutoff: datetime, limit: int) -> list[dict[str, Any]]:
    """Eligible entries that haven't been enriched since `cutoff`.

    "Recently enriched" is detected by a task_outcomes row with
    task_kind='enrich_tags', object_kind='file_entry', object_id=<entry_id>,
    completed_at >= cutoff. DESIGN.md §14.3 forbids reading audit_events
    for business logic.
    """
    async with session_scope() as session:
        rows = await entries_repo.list_active_with_file_eligible_for_enrich(
            session, recent_cutoff=cutoff, limit=limit,
        )

        candidates: list[dict[str, Any]] = []
        for entry, file_row in rows:
            existing_tags = await entry_tags_repo.list_existing_for_entry(
                session, entry.id,
            )
            candidates.append({
                "entry_id": entry.id,
                "display_name": entry.display_name,
                "extra": entry.extra,
                "file_summary": file_row.summary,
                "file_description": file_row.description,
                "file_kind": file_row.kind,
                "existing_tag_ids": [r[0] for r in existing_tags],
                "existing_tag_summary": [
                    {"name": r[1], "facet": r[2]} for r in existing_tags
                ],
            })
        await session.commit()
    return candidates


async def _load_vocabulary() -> dict[str, list[dict[str, Any]]]:
    async with session_scope() as session:
        out: dict[str, list[dict[str, Any]]] = {}
        for facet in ("topic", "form", "time", "source", "language", "extra"):
            rows = await tags_repo.list_canonical_per_facet(
                session, facet=facet, limit=TAG_VOCAB_TOP_PER_FACET,
            )
            if rows:
                out[facet] = [
                    {"id": r[0], "name": r[1], "doc_count": r[2] or 0}
                    for r in rows
                ]
        await session.commit()
    return out


async def _ask_llm_for_batch(
    batch: list[dict[str, Any]],
    vocabulary: dict[str, list[dict[str, Any]]],
) -> dict[str, list[str]]:
    user_payload = {
        "entries": [
            {
                "entry_id": e["entry_id"],
                "display_name": e["display_name"],
                "kind": e["file_kind"],
                "summary": e["file_summary"],
                "description_sketch": _sketch_description(e["file_description"]),
                "extra": e["extra"],
                "existing_tags": e["existing_tag_summary"],
            }
            for e in batch
        ],
        "vocabulary": vocabulary,
    }
    user_text = (
        "For each entry, pick additional tag IDs from the vocabulary that "
        "should be attached. Empty list is preferred over noise.\n\n"
        f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>"
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=ENRICH_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        temperature=0.1,
    ))
    out: dict[str, list[str]] = {}
    tagged = parse_tagged(resp.text or "")
    block = tagged.get("assignments", "")
    if not block:
        log.warning("enrich_tags: no <assignments> block in response")
        return out
    kv = parse_kv(block)
    for entry_id, tag_csv in kv.items():
        tag_ids = [t.strip() for t in tag_csv.split(",") if t.strip()]
        out[entry_id] = tag_ids
    return out


def _sketch_description(description: Any) -> Any:
    """Trim description JSON for the prompt — sections' headings + key terms only.
    The original text body is in storage; the LLM does not need it for tagging."""
    if not isinstance(description, dict):
        return description
    secs = description.get("sections")
    if not isinstance(secs, list):
        return description
    return {
        "sections": [
            {"title": s.get("title"), "key_terms": s.get("key_terms", [])}
            for s in secs[:20]
        ]
    }


async def _apply_assignments(
    *,
    batch_entry_ids: list[str],
    assignments: dict[str, list[str]],
    valid_tag_ids: set[str],
    now: datetime,
) -> int:
    """Insert entry_tags for accepted assignments. Returns added count."""
    added = 0
    async with session_scope() as session:
        for entry_id in batch_entry_ids:
            picks = assignments.get(entry_id) or []
            picks = [t for t in picks if t in valid_tag_ids]

            existing_tag_ids: set[str] = set(
                await entry_tags_repo.list_tag_ids_for_entry(session, entry_id)
            )

            # Dedup within the call: LLM may emit the same id twice. Preserve
            # first-occurrence order so the entry_enriched audit reads sensibly.
            seen_in_call: set[str] = set()
            new_picks: list[str] = []
            for t in picks:
                if t in existing_tag_ids or t in seen_in_call:
                    continue
                seen_in_call.add(t)
                new_picks.append(t)

            for tag_id in new_picks:
                session.add(EntryTag(
                    entry_id=entry_id,
                    tag_id=tag_id,
                    source="enrich_tags",
                    created_at=now,
                ))
                added += 1

            await record_outcome(
                session,
                task_kind=ENRICH_TASK_KIND,
                object_kind="file_entry",
                object_id=entry_id,
                outcome="applied" if new_picks else "noop",
                detail={
                    "tag_ids_added": new_picks,
                    "tag_ids_proposed_but_dropped": [
                        t for t in (assignments.get(entry_id) or [])
                        if t not in valid_tag_ids
                    ],
                    "tag_ids_already_present": [
                        t for t in picks if t in existing_tag_ids
                    ],
                },
            )
        await session.commit()
    return added
