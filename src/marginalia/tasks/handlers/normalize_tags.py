"""normalize_tags — design.md §9.1 + §9.4 + §14.4.

LLM-driven controlled-vocabulary maintenance. Per facet, asks the LLM to
identify synonym groups; applies the merges:

  - chosen canonical: alias_of stays NULL (or, if it was an alias, is reset)
  - other tags in the group: alias_of = canonical.id
  - one tag_aliases row per merged-in NAME (history is permanent — design
    §14.4 #2)
  - entry_tags rows pointing at any non-canonical member are rewritten to
    point at the canonical id (DELETE conflicting + UPDATE distinct, since
    the PK is composite (entry_id, tag_id))
  - tags.doc_count is recomputed for every canonical tag at the end

Invariants enforced:
  - alias_of must point at a tag whose own alias_of IS NULL (§14.4 #3 — no
    chained aliases). The merge logic always picks an alias_of=NULL canonical
    and resets any chained values inside the group.
  - tag_aliases is INSERT-only.
  - entry_tags row uniqueness preserved across the rewrite.
  - Audit: one `tag_merged` per merged tag + one task_outcomes row
    (object_kind='global') summarizing the run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import delete, func, select, update

from marginalia.db.models import EntryTag, Tag, TagAlias
from marginalia.db.session import session_scope
from marginalia.llm import ChatMessage, ChatRequest, TextBlock, get_chat_client
from marginalia.services.audit import write_event
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_NORMALIZE_TAGS, task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

FACETS = ("topic", "form", "time", "source", "language", "extra")
BATCH_SIZE = 60  # per-facet tag count cap fed to the model in one call
MIN_TAGS_TO_NORMALIZE = 2  # one LLM call per facet, even tiny ones (cheap; might find a real synonym)


NORMALIZE_SYSTEM = """You are Marginalia's tag-vocabulary editor.

Given a list of tags within ONE facet, identify groups of synonymous tags and
choose a canonical member for each group. Be conservative — false merges are
costly and irreversible from the user's view.

Output a JSON object:
  {"merges": [{"canonical_id": "...", "merge_in_ids": ["...", "..."]}]}

Rules:
  - canonical_id MUST be one of the supplied tag ids
  - merge_in_ids MUST also all be supplied ids, NEVER include canonical_id
  - leave the rest of the tags alone — do NOT include groups of size 1
  - merging across distinct concepts (e.g. "ML" and "AI" — overlapping but
    not identical) is FORBIDDEN; only merge true synonyms / spelling variants
    / case differences / ASCII↔Unicode differences
  - if no clean merges exist, return {"merges": []}
"""


NORMALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["merges"],
    "properties": {
        "merges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["canonical_id", "merge_in_ids"],
                "properties": {
                    "canonical_id": {"type": "string"},
                    "merge_in_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                },
            },
        },
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_NORMALIZE_TAGS)
async def handle_normalize_tags(payload: Mapping[str, Any]) -> None:
    total_merges_applied = 0
    total_tags_redirected = 0
    facets_processed: list[str] = []

    for facet in FACETS:
        per_facet = await _normalize_one_facet(facet)
        if per_facet is None:
            continue
        facets_processed.append(facet)
        total_merges_applied += per_facet["merges_applied"]
        total_tags_redirected += per_facet["tags_redirected"]

    # Recompute doc_count for ALL tags after merges are applied.
    async with session_scope() as session:
        await _recompute_doc_counts(session)
        await record_outcome(
            session,
            task_kind=KIND_NORMALIZE_TAGS,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if total_merges_applied else "noop",
            detail={
                "facets_processed": facets_processed,
                "merges_applied": total_merges_applied,
                "tags_redirected": total_tags_redirected,
            },
        )
        await session.commit()

    if total_merges_applied:
        log.info(
            "normalize_tags: %d merges across %d facets, %d entry_tags rewritten",
            total_merges_applied, len(facets_processed), total_tags_redirected,
        )


async def _normalize_one_facet(facet: str) -> dict[str, int] | None:
    """Return None if the facet was skipped (too few tags), else stats dict."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(Tag.id, Tag.name, Tag.alias_of, Tag.doc_count)
                .where(Tag.facet == facet)
                .order_by(Tag.doc_count.desc(), Tag.name)
            )
        ).all()
        canonical_only = [r for r in rows if r[2] is None]
        await session.commit()

    if len(canonical_only) < MIN_TAGS_TO_NORMALIZE:
        return None

    # Feed only canonical tags to the LLM (we never re-merge already-aliased
    # ones; merges happen between canonicals).
    batch = canonical_only[:BATCH_SIZE]
    payload_for_llm = [
        {"id": r[0], "name": r[1], "doc_count": r[3] or 0}
        for r in batch
    ]
    user_text = (
        f"Facet: {facet}\n\nTags ({len(payload_for_llm)} total):\n"
        f"{json.dumps(payload_for_llm, ensure_ascii=False)}\n\n"
        "Identify synonym groups. Output JSON per the schema."
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=NORMALIZE_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=2048,
        json_schema=NORMALIZE_SCHEMA,
        temperature=0.1,
    ))
    if resp.parsed_json is None:
        log.warning("normalize_tags(%s): non-JSON response, skipping facet", facet)
        return None

    merges = resp.parsed_json.get("merges") or []
    if not merges:
        return {"merges_applied": 0, "tags_redirected": 0}

    valid_ids = {r[0] for r in batch}

    # Apply merges in a single transaction so any FK / PK issue rolls back.
    async with session_scope() as session:
        merges_applied = 0
        tags_redirected = 0
        for merge in merges:
            canonical_id = merge.get("canonical_id")
            merge_in_ids = list(merge.get("merge_in_ids") or [])
            if canonical_id is None or canonical_id not in valid_ids:
                continue
            if canonical_id in merge_in_ids:
                merge_in_ids = [m for m in merge_in_ids if m != canonical_id]
            merge_in_ids = [m for m in merge_in_ids if m in valid_ids]
            if not merge_in_ids:
                continue

            redirected = await _apply_one_merge(
                session,
                canonical_id=canonical_id,
                merge_in_ids=merge_in_ids,
                facet=facet,
            )
            tags_redirected += redirected
            merges_applied += 1

        await session.commit()

    return {"merges_applied": merges_applied, "tags_redirected": tags_redirected}


async def _apply_one_merge(
    session,
    *,
    canonical_id: str,
    merge_in_ids: Iterable[str],
    facet: str,
) -> int:
    """Apply a single merge group. Returns count of entry_tags rewritten."""
    now = _utcnow()
    canonical = await session.get(Tag, canonical_id)
    if canonical is None:
        return 0
    # Defensive: if the picked canonical is itself an alias, walk to its root
    # so we never create a chained alias_of.
    if canonical.alias_of is not None:
        root = await session.get(Tag, canonical.alias_of)
        if root is None or root.alias_of is not None:
            return 0
        canonical = root
        canonical_id = root.id

    entry_tags_redirected = 0

    for merge_id in merge_in_ids:
        if merge_id == canonical_id:
            continue
        merged = await session.get(Tag, merge_id)
        if merged is None or merged.facet != facet:
            continue

        # 1. Append history row (NEVER deleted)
        session.add(TagAlias(
            id=new_id(),
            from_name=merged.name,
            to_tag_id=canonical_id,
            note=None,
            created_at=now,
        ))

        # 2. Rewrite entry_tags (entry_id, tag_id) PK requires careful merge.
        #    Find rows pointing at the merged tag where the SAME entry already
        #    has the canonical tag → DELETE the redundant ones. The remaining
        #    rows we UPDATE to point at canonical_id.
        await session.execute(
            delete(EntryTag).where(
                EntryTag.tag_id == merge_id,
                EntryTag.entry_id.in_(
                    select(EntryTag.entry_id).where(EntryTag.tag_id == canonical_id)
                ),
            )
        )
        result = await session.execute(
            update(EntryTag)
            .where(EntryTag.tag_id == merge_id)
            .values(tag_id=canonical_id)
        )
        entry_tags_redirected += int(result.rowcount or 0)

        # 3. Mark the merged tag as an alias and update mtime.
        merged.alias_of = canonical_id
        merged.updated_at = now

        await write_event(
            session,
            kind="tag_merged",
            payload={
                "facet": facet,
                "canonical_id": canonical_id,
                "canonical_name": canonical.name,
                "merged_tag_id": merge_id,
                "merged_tag_name": merged.name,
                "entry_tags_redirected": int(result.rowcount or 0),
            },
        )

    canonical.last_used_at = now
    canonical.updated_at = now
    return entry_tags_redirected


async def _recompute_doc_counts(session) -> None:
    """Set tags.doc_count = COUNT(entry_tags WHERE tag_id = tags.id) for every tag.

    Aliases keep their last value (entry_tags should no longer point at them,
    so their counts will read 0 — that's correct).
    """
    rows = (
        await session.execute(
            select(EntryTag.tag_id, func.count())
            .group_by(EntryTag.tag_id)
        )
    ).all()
    counts: dict[str, int] = {tag_id: c for tag_id, c in rows}

    all_tag_ids = (await session.execute(select(Tag.id))).scalars().all()
    for tag_id in all_tag_ids:
        await session.execute(
            update(Tag)
            .where(Tag.id == tag_id)
            .values(doc_count=counts.get(tag_id, 0))
        )
