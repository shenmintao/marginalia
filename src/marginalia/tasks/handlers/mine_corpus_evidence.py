"""mine_corpus_evidence — corpus-driven entry-relation mining with LLM gating.

Companion to mine_session_cooccurrence. Where the latter is purely
usage-driven (journal-based statistical), this task is structurally
guided: we sample candidate (entry_a, entry_b) pairs from the corpus
itself — pairs that share a catalog subtree or any tag — and ask the
LLM to judge whether the pair is genuinely related. Accept → write a
new entry_relation; reject → record the reason in task_outcomes so
this pair is not re-evaluated until the corpus changes.

Hard rules (philosophy boundary, see prior cycle discussion):
  - Pairs already evaluated (any task_outcomes row with object_kind
    'entry_pair' and object_id "{a}|{b}" exists) are NEVER re-fed to
    the LLM. This is the key cost guard — the corpus pool is huge but
    the per-pair LLM cost is paid once.
  - Pairs already linked by an entry_relation (any source_kind) are
    skipped — we don't second-guess existing relations.
  - Either entry being soft-deleted, lifecycle ∈ {demoted, archived,
    manual_archived} excludes the pair.
  - Generation pool is bounded; per run we evaluate up to MAX_PAIRS
    (default 30); the LLM sees them all in one batch.
  - Reject reasons are stored verbatim in task_outcomes.detail.reason
    so a future audit / debug pass can see why mining declined.

Inputs:
  payload (all optional):
    "max_pairs" int, "min_shared_signal" int (1 default — pair must
    share at least one tag OR be in same catalog subtree), "dry_run" bool.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping

from marginalia.db.models import (
    EntryRelation,
)
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    get_chat_client,
)
from marginalia.llm.tagged_response import parse_kv, parse_tagged
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_relations as relations_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
    select_object_ids,
)
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

MAX_PAIRS = 30
SOURCE_KIND = "mine_corpus_evidence"


CORPUS_MINE_SYSTEM = """You are Marginalia's corpus-evidence reviewer.

The system samples candidate entry pairs from the knowledge base based
on structural co-location (shared catalog subtree or shared tag). For
each pair, decide whether they are genuinely related — meaning a future
investigator should be able to discover one from the other.

Be conservative. Most structurally co-located pairs are NOT meaningful
relations — they're just neighbors. Only accept pairs whose summaries,
descriptions, or tags reveal a real semantic link (same project,
contradicting findings, build on each other, expand on each other,
etc.). When in doubt, reject.

Output format — exactly one block, one line per pair:

  <decisions>
  <pair_id>: accept - one short reason
  <pair_id>: reject - one short reason
  </decisions>

Use the pair_id values verbatim from the input. The decision MUST be
either `accept` or `reject`. Separate decision and reason with ` - `
(space, dash, space). Every input pair MUST appear in your output —
do not invent additional pairs and do not skip any. Do NOT wrap in
JSON or add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
CORPUS_MINE_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _pair_id(a: str, b: str) -> str:
    pa, pb = _pair_key(a, b)
    return f"{pa}|{pb}"


async def handle_mine_corpus_evidence(payload: Mapping[str, Any]) -> None:
    max_pairs = int(payload.get("max_pairs") or MAX_PAIRS)
    dry_run = bool(payload.get("dry_run") or False)

    candidates = await _build_candidate_pool(max_pairs=max_pairs)
    if not candidates:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind="mine_corpus_evidence",
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"candidates": 0,
                        "reason": "no eligible structurally-linked pairs"},
            )
            await session.commit()
        return

    decisions = await _ask_llm_for_decisions(candidates)
    if not decisions:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind="mine_corpus_evidence",
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="rejected",
                detail={"candidates": len(candidates),
                        "reason": "LLM returned no parseable JSON"},
            )
            await session.commit()
        return

    accepted = 0
    rejected = 0
    skipped = 0
    by_pair_id: dict[str, dict[str, Any]] = {c["pair_id"]: c for c in candidates}

    async with session_scope() as session:
        for d in decisions:
            pair_id = d.get("pair_id") or ""
            decision = d.get("decision") or "reject"
            reason = (d.get("reason") or "").strip() or "(no reason given)"
            cand = by_pair_id.get(pair_id)
            if cand is None:
                skipped += 1
                continue
            entry_a, entry_b = cand["entry_a_id"], cand["entry_b_id"]

            if decision == "accept" and not dry_run:
                rel_id = new_id()
                session.add(EntryRelation(
                    id=rel_id,
                    entry_a_id=entry_a,
                    entry_b_id=entry_b,
                    note=reason,
                    source_kind=SOURCE_KIND,
                    last_observed_at=_utcnow(),
                    observation_count=1,
                    created_at=_utcnow(),
                ))
                accepted += 1
                await audit_events_repo.append(
                    session, kind="relation_mined",
                    payload={
                        "relation_id": rel_id,
                        "entry_a_id": entry_a, "entry_b_id": entry_b,
                        "source_kind": SOURCE_KIND,
                        "action": "created",
                    },
                )
                await record_outcome(
                    session,
                    task_kind="mine_corpus_evidence",
                    object_kind="entry_pair", object_id=pair_id,
                    outcome="applied",
                    detail={
                        "entry_a_id": entry_a, "entry_b_id": entry_b,
                        "decision": "accept", "reason": reason,
                        "shared_signal": cand["shared_signal"],
                    },
                )
            else:
                rejected += 1
                await record_outcome(
                    session,
                    task_kind="mine_corpus_evidence",
                    object_kind="entry_pair", object_id=pair_id,
                    outcome="rejected",
                    detail={
                        "entry_a_id": entry_a, "entry_b_id": entry_b,
                        "decision": "reject", "reason": reason,
                        "shared_signal": cand["shared_signal"],
                    },
                )

        await record_outcome(
            session,
            task_kind="mine_corpus_evidence",
            object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if accepted else ("noop" if not rejected else "rejected"),
            detail={
                "candidates": len(candidates),
                "accepted": accepted, "rejected": rejected,
                "missing_decisions": skipped,
                "dry_run": dry_run,
            },
        )
        await session.commit()

    log.info("mine_corpus_evidence: pool=%d accepted=%d rejected=%d",
             len(candidates), accepted, rejected)


async def _build_candidate_pool(*, max_pairs: int) -> list[dict[str, Any]]:
    """Sample structurally-linked pairs not previously evaluated/related."""
    async with session_scope() as session:
        already_evaluated_ids = await select_object_ids(
            session,
            task_kind="mine_corpus_evidence",
            object_kind="entry_pair",
        )
        existing_pairs: set[str] = {
            f"{a}|{b}" for a, b in await relations_repo.list_pair_keys(session)
        }

        live_entries = await entries_repo.list_live_active_with_file(session)
        if len(live_entries) < 2:
            await session.commit()
            return []

        # tag map: entry_id -> set of tag_id
        tag_rows = await entry_tags_repo.list_tag_ids_for_entries(
            session, [e.id for e, _ in live_entries],
        )
        tags_by_entry: dict[str, set[str]] = {}
        for eid, tid in tag_rows:
            tags_by_entry.setdefault(eid, set()).add(tid)

        # catalog subtree map: entry_id -> set of ancestor catalog_id (incl self)
        cat_subtree_by_entry = await _catalog_ancestors(session, live_entries)

        candidates: list[dict[str, Any]] = []
        seen_pair_ids: set[str] = set()

        # We iterate pairs in a deterministic order (by entry_id) — fully
        # exploring the full O(N^2) is expensive for large corpora, but
        # max_pairs is the early exit.
        rows = sorted(live_entries, key=lambda x: x[0].id)
        for i in range(len(rows)):
            if len(candidates) >= max_pairs:
                break
            ea, fa = rows[i]
            for j in range(i + 1, len(rows)):
                if len(candidates) >= max_pairs:
                    break
                eb, fb = rows[j]
                pid = _pair_id(ea.id, eb.id)
                if pid in seen_pair_ids or pid in existing_pairs or pid in already_evaluated_ids:
                    continue

                shared_tags = tags_by_entry.get(ea.id, set()) & tags_by_entry.get(eb.id, set())
                shared_cats = cat_subtree_by_entry.get(ea.id, set()) & cat_subtree_by_entry.get(eb.id, set())
                if not shared_tags and not shared_cats:
                    continue

                seen_pair_ids.add(pid)
                shared_signal = []
                if shared_tags:
                    shared_signal.append(f"shared_tag_count={len(shared_tags)}")
                if shared_cats:
                    shared_signal.append(f"shared_catalog_count={len(shared_cats)}")
                candidates.append({
                    "pair_id": pid,
                    "entry_a_id": ea.id if ea.id < eb.id else eb.id,
                    "entry_b_id": eb.id if ea.id < eb.id else ea.id,
                    "entry_a_summary": (fa.summary or "")[:300],
                    "entry_b_summary": (fb.summary or "")[:300],
                    "entry_a_display_name": ea.display_name,
                    "entry_b_display_name": eb.display_name,
                    "entry_a_kind": fa.kind,
                    "entry_b_kind": fb.kind,
                    "shared_signal": shared_signal,
                })
        await session.commit()
    return candidates


async def _catalog_ancestors(session, live_entries) -> dict[str, set[str]]:
    """Return entry_id -> set of catalog_id ancestors (incl. self) for each
    entry that has a catalog assignment. Entries with NULL catalog_id get
    an empty set."""
    out: dict[str, set[str]] = {}
    cat_ids = {e.catalog_id for e, _ in live_entries if e.catalog_id is not None}
    if not cat_ids:
        return out
    parent_of: dict[str, str | None] = {}
    rows = await catalogs_repo.list_live_id_parent(session)
    for cid, pid in rows:
        parent_of[cid] = pid
    for e, _ in live_entries:
        if e.catalog_id is None:
            continue
        ancestors: set[str] = set()
        cur: str | None = e.catalog_id
        while cur is not None and cur not in ancestors:
            ancestors.add(cur)
            cur = parent_of.get(cur)
        out[e.id] = ancestors
    return out


async def _ask_llm_for_decisions(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    user_payload = {
        "pairs": [
            {
                "pair_id": c["pair_id"],
                "entry_a": {
                    "name": c["entry_a_display_name"],
                    "kind": c["entry_a_kind"],
                    "summary": c["entry_a_summary"],
                },
                "entry_b": {
                    "name": c["entry_b_display_name"],
                    "kind": c["entry_b_kind"],
                    "summary": c["entry_b_summary"],
                },
                "structural_signal": c["shared_signal"],
            }
            for c in candidates
        ],
    }
    user_text = (
        "Review these structurally-co-located entry pairs. Decide which "
        "are genuinely related vs. mere neighbors.\n\n"
        f"<pairs>\n{json.dumps(user_payload, ensure_ascii=False)}\n</pairs>"
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=CORPUS_MINE_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        temperature=0.1,
    ))
    tagged = parse_tagged(resp.text or "")
    block = tagged.get("decisions", "")
    if not block:
        log.warning("mine_corpus_evidence: no <decisions> block in response")
        return []
    kv = parse_kv(block)
    out: list[dict[str, Any]] = []
    for pair_id, value in kv.items():
        decision, sep, reason = value.partition(" - ")
        decision = decision.strip().lower()
        if decision not in ("accept", "reject"):
            continue
        out.append({
            "pair_id": pair_id,
            "decision": decision,
            "reason": reason.strip() if sep else "",
        })
    return out
