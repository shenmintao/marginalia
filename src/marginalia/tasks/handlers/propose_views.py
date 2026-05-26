"""propose_views — corpus-driven view discovery (Cycle 24).

Finds tag clusters that look like they deserve their own saved view but
don't have one yet. A "cluster" here is a set of canonical tags such
that ≥ MIN_ENTRIES_PER_CLUSTER active entries each carry ALL tags in
the set, AND the set has ≥ MIN_TAGS_PER_CLUSTER tags. The LLM gates
each cluster — it picks the view name, summary, and filter_spec, or
rejects the cluster as not worth a view.

Boundary rules:
  - Already-covered clusters (any existing view whose filter_spec.tags_all
    overlaps the cluster by ≥ COVER_OVERLAP threshold) are skipped — we
    don't propose duplicates.
  - Already-evaluated clusters (task_outcomes row with object_kind=
    'view_proposal' and our cluster_id) are skipped — re-runs don't
    re-feed the LLM.
  - cap 5 new views per run.

Writes:
  - views (INSERT) per accepted cluster
  - audit_events 'view_created' per accepted view
  - task_outcomes per cluster ('applied' or 'rejected') + global summary

Cluster id (used as task_outcomes.object_id): a deterministic hash of
the sorted tag ids in the cluster. Same cluster shape always maps to
the same id, so re-evaluation guard works correctly.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from typing import Any, Mapping

from marginalia.db.models import (
    View,
)
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage, ChatRequest, TextBlock, get_chat_client,
)
from marginalia.llm.tagged_response import parse_tagged
from marginalia.repositories import entry_relations as relations_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories import views as views_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
    select_object_ids,
)
from marginalia.tasks.kinds import KIND_PROPOSE_VIEWS, task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

MIN_TAGS_PER_CLUSTER = 3
MIN_ENTRIES_PER_CLUSTER = 10
MAX_CLUSTERS_TO_EVALUATE = 8
MAX_VIEWS_PER_RUN = 5
COVER_OVERLAP_RATIO = 0.8


PROPOSE_VIEWS_SYSTEM = """You are Marginalia's view proposer.

The system samples tag clusters: sets of tags that frequently co-occur
on entries but have no saved view yet. For each cluster, decide:
  - Is it a real topic worth materializing as a saved view?
  - If yes, what should the view be called and what filter should
    define it?

Be conservative. Only accept clusters whose tag combination clearly
identifies a useful topic. Reject clusters that are too generic (e.g.
"text + english + 2024" is a non-topic), too narrow, or already
covered by other views you can see.

Output format — exactly one block, one compact JSON object per line,
one line per cluster:

  <decisions>
  {"cluster_id":"...", "decision":"accept", "reason":"...", "name":"...", "summary":"...", "filter_tag_ids":["..."]}
  {"cluster_id":"...", "decision":"reject", "reason":"..."}
  </decisions>

For accepted clusters include `name` (≤ 40 chars), `summary` (one
sentence), and `filter_tag_ids` (subset of the cluster's tag ids; you
may drop noisy ones, typically include all). Use the cluster_id values
verbatim from the input. Do NOT wrap the whole output in an outer JSON
object and do NOT add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
PROPOSE_VIEWS_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _cluster_id_of(tag_ids: list[str]) -> str:
    """Deterministic id for a tag set (order-independent)."""
    canonical = "|".join(sorted(tag_ids))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:24]


@task_handler(KIND_PROPOSE_VIEWS)
async def handle_propose_views(payload: Mapping[str, Any]) -> None:
    min_tags = int(payload.get("min_tags") or MIN_TAGS_PER_CLUSTER)
    min_entries = int(payload.get("min_entries") or MIN_ENTRIES_PER_CLUSTER)
    max_clusters = int(payload.get("max_clusters") or MAX_CLUSTERS_TO_EVALUATE)
    cap = int(payload.get("cap") or MAX_VIEWS_PER_RUN)

    candidates = await _build_candidate_clusters(
        min_tags=min_tags,
        min_entries=min_entries,
        max_clusters=max_clusters,
    )
    if not candidates:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_PROPOSE_VIEWS,
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"clusters": 0, "reason": "no eligible clusters"},
            )
            await session.commit()
        return

    decisions = await _ask_llm_for_decisions(candidates)
    if not decisions:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_PROPOSE_VIEWS,
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="rejected",
                detail={"clusters": len(candidates),
                        "reason": "LLM returned no parseable JSON"},
            )
            await session.commit()
        return

    accepted = 0
    rejected = 0
    by_cluster: dict[str, dict[str, Any]] = {c["cluster_id"]: c for c in candidates}

    async with session_scope() as session:
        for d in decisions:
            cid = d.get("cluster_id") or ""
            cand = by_cluster.get(cid)
            if cand is None:
                continue
            decision = d.get("decision") or "reject"
            reason = (d.get("reason") or "").strip() or "(no reason given)"

            if decision == "accept" and accepted < cap:
                view = await _persist_view(
                    session,
                    cluster=cand,
                    name=(d.get("name") or "").strip(),
                    summary=(d.get("summary") or "").strip(),
                    filter_tag_ids=list(d.get("filter_tag_ids") or cand["tag_ids"]),
                    reason=reason,
                )
                accepted += 1
                await record_outcome(
                    session,
                    task_kind=KIND_PROPOSE_VIEWS,
                    object_kind="view_proposal", object_id=cid,
                    outcome="applied",
                    detail={
                        "cluster_tag_ids": cand["tag_ids"],
                        "entry_count": cand["entry_count"],
                        "view_id": view.id,
                        "view_name": view.name,
                        "decision": "accept",
                        "reason": reason,
                    },
                )
            else:
                rejected += 1
                await record_outcome(
                    session,
                    task_kind=KIND_PROPOSE_VIEWS,
                    object_kind="view_proposal", object_id=cid,
                    outcome="rejected",
                    detail={
                        "cluster_tag_ids": cand["tag_ids"],
                        "entry_count": cand["entry_count"],
                        "decision": "reject",
                        "reason": reason,
                    },
                )

        await record_outcome(
            session,
            task_kind=KIND_PROPOSE_VIEWS,
            object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if accepted else ("noop" if not rejected else "rejected"),
            detail={
                "clusters": len(candidates),
                "accepted": accepted,
                "rejected": rejected,
            },
        )
        await session.commit()

    log.info("propose_views: clusters=%d accepted=%d rejected=%d",
             len(candidates), accepted, rejected)


async def _persist_view(
    session,
    *,
    cluster: dict[str, Any],
    name: str,
    summary: str,
    filter_tag_ids: list[str],
    reason: str,
) -> View:
    valid_ids = set(cluster["tag_ids"])
    filter_tag_ids = [t for t in filter_tag_ids if t in valid_ids]
    if not filter_tag_ids:
        filter_tag_ids = list(cluster["tag_ids"])

    if not name:
        name = f"Cluster {cluster['cluster_id'][:8]}"

    view = View(
        id=new_id(),
        name=name[:255],
        summary=summary[:255] if summary else None,
        description=None,
        extra=None,
        tags=cluster["tag_names"],
        filter_spec={
            "tags_all": filter_tag_ids,
            "lifecycle": ["active", "manual_active"],
        },
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    session.add(view)
    await session.flush()
    await audit_events_repo.append(
        session,
        kind="view_created",
        payload={
            "view_id": view.id,
            "name": view.name,
            "filter_tag_ids": filter_tag_ids,
            "source_kind": "propose_views",
            "cluster_id": cluster["cluster_id"],
            "entry_count_at_creation": cluster["entry_count"],
            "reason": reason,
        },
    )
    return view


async def _build_candidate_clusters(
    *,
    min_tags: int,
    min_entries: int,
    max_clusters: int,
) -> list[dict[str, Any]]:
    """Find tag combinations that ≥ min_entries entries share."""
    async with session_scope() as session:
        # entry → tag_ids (active live entries only)
        rows = await entry_tags_repo.list_live_active_entry_tag_pairs(session)
        entry_tags: dict[str, set[str]] = {}
        for eid, tid in rows:
            entry_tags.setdefault(eid, set()).add(tid)

        # Tag id → name (canonical only — alias_of is null)
        tag_rows = await tags_repo.list_canonical_id_name(session)
        tag_name_by_id: dict[str, str] = dict(tag_rows)
        canonical_tag_ids = set(tag_name_by_id)

        # Restrict each entry's tag set to canonical tags
        for eid, tags in list(entry_tags.items()):
            entry_tags[eid] = tags & canonical_tag_ids

        # Existing views' tag_all sets — for "already covered" exclusion
        existing_view_tag_sets: list[frozenset[str]] = []
        view_rows = await views_repo.list_all(session)
        for v in view_rows:
            spec = v.filter_spec or {}
            existing_view_tag_sets.append(
                frozenset(spec.get("tags_all") or [])
            )

        # Already-evaluated cluster ids
        evaluated_ids = await select_object_ids(
            session,
            task_kind=KIND_PROPOSE_VIEWS,
            object_kind="view_proposal",
        )

        # Generate cluster candidates: for each tag, find which entries
        # have it; then look at frequent co-occurring tag triples.
        # Heuristic: take top tags by doc_count, enumerate combos of
        # min_tags from them, and count entries having ALL.
        # This avoids exploring the full 2^N power set.
        tag_doc_count: Counter[str] = Counter()
        for tags in entry_tags.values():
            for t in tags:
                tag_doc_count[t] += 1

        # Top tags by count (must each have at least min_entries)
        top_tags = [
            t for t, c in tag_doc_count.most_common()
            if c >= min_entries
        ]

        seen_clusters: set[str] = set()
        candidates: list[dict[str, Any]] = []

        # Source 1: tag-cooccurrence clusters. Walk tag combos of size
        # min_tags from top_tags by doc_count.
        for combo in combinations(top_tags, min_tags):
            combo_set = frozenset(combo)
            cid = _cluster_id_of(list(combo))
            if cid in seen_clusters or cid in evaluated_ids:
                continue
            # Count entries having all tags in combo
            count = sum(
                1 for tags in entry_tags.values()
                if combo_set.issubset(tags)
            )
            if count < min_entries:
                continue
            # Already-covered check: if any existing view's tag set
            # overlaps this cluster by ≥ COVER_OVERLAP_RATIO, skip.
            if _is_covered(combo_set, existing_view_tag_sets):
                continue

            seen_clusters.add(cid)
            candidates.append({
                "cluster_id": cid,
                "tag_ids": sorted(combo),
                "tag_names": sorted(tag_name_by_id[t] for t in combo),
                "entry_count": count,
                "source": "tag_cooccurrence",
            })
            if len(candidates) >= max_clusters:
                break

        # Source 2: relation-graph clusters. Connected components on
        # vetted=True entry_relations surface "tightly linked but
        # catalog-scattered" entry groups the tag-only walk above
        # might miss. Each component's union of tags becomes the
        # cluster's candidate filter spec.
        if len(candidates) < max_clusters:
            relation_clusters = await _build_relation_clusters(
                session,
                entry_tags=entry_tags,
                tag_name_by_id=tag_name_by_id,
                min_entries=min_entries,
                min_tags=min_tags,
                evaluated_ids=evaluated_ids,
                seen_clusters=seen_clusters,
                existing_view_tag_sets=existing_view_tag_sets,
                cap=max_clusters - len(candidates),
            )
            candidates.extend(relation_clusters)
        await session.commit()
    return candidates


async def _build_relation_clusters(
    session,
    *,
    entry_tags: dict[str, set[str]],
    tag_name_by_id: dict[str, str],
    min_entries: int,
    min_tags: int,
    evaluated_ids: set[str],
    seen_clusters: set[str],
    existing_view_tag_sets: list[frozenset[str]],
    cap: int,
) -> list[dict[str, Any]]:
    """Find connected components on the vetted relation graph and turn
    each into a candidate cluster.

    The intuition: when several entries are tightly linked by vetted
    cooccurrence/citation/tag-overlap signals AND share at least
    min_tags tags, that's a "naturally clustered topic" the corpus is
    telling us about — independent of whatever the tag-only walk
    surfaces. Especially valuable when the cluster spans catalog
    branches (bridging a topic across a structural boundary).

    A component is only kept as a cluster if its members share a
    common-enough tag set (>= min_tags) and have enough mass (>=
    min_entries entries). The cluster's `filter_tag_ids` is the
    intersection of tags across the component's members; the LLM
    then accepts/rejects and renames as usual.
    """
    rows = await relations_repo.list_vetted_pair_keys(session)
    if not rows:
        return []

    # Live entry filter: only walk edges whose endpoints are still in
    # entry_tags (which already excludes soft-deleted + non-active).
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in rows:
        if a not in entry_tags or b not in entry_tags:
            continue
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    # Bucket members by component root.
    components: dict[str, list[str]] = {}
    for eid in parent:
        components.setdefault(find(eid), []).append(eid)

    out: list[dict[str, Any]] = []
    for members in components.values():
        if len(members) < min_entries:
            continue
        # Intersection of tag sets — the cluster's "shared identity".
        shared = set(entry_tags[members[0]])
        for m in members[1:]:
            shared &= entry_tags[m]
            if len(shared) < min_tags:
                break
        if len(shared) < min_tags:
            continue
        # Cluster id is hash over (tag_ids + "rel" suffix) so the same
        # tag set from two sources doesn't collide.
        tag_ids = sorted(shared)
        cid = _cluster_id_of(tag_ids) + "_rel"
        if cid in seen_clusters or cid in evaluated_ids:
            continue
        if _is_covered(frozenset(tag_ids), existing_view_tag_sets):
            continue
        seen_clusters.add(cid)
        out.append({
            "cluster_id": cid,
            "tag_ids": tag_ids,
            "tag_names": sorted(tag_name_by_id[t] for t in tag_ids),
            "entry_count": len(members),
            "source": "relation_graph",
        })
        if len(out) >= cap:
            break
    return out


def _is_covered(
    cluster: frozenset[str],
    view_tag_sets: list[frozenset[str]],
) -> bool:
    """A cluster is 'covered' if some existing view's tags_all overlaps
    cluster's tag set by ≥ COVER_OVERLAP_RATIO of cluster size."""
    if not cluster:
        return True
    threshold = max(1, int(len(cluster) * COVER_OVERLAP_RATIO))
    for vts in view_tag_sets:
        if not vts:
            continue
        if len(cluster & vts) >= threshold:
            return True
    return False


async def _ask_llm_for_decisions(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload = {
        "clusters": [
            {
                "cluster_id": c["cluster_id"],
                "tag_ids": c["tag_ids"],
                "tag_names": c["tag_names"],
                "entry_count": c["entry_count"],
            }
            for c in candidates
        ],
    }
    user_text = (
        "Review these tag clusters. For each, decide whether the system "
        "should create a saved view collecting entries that match.\n\n"
        f"<clusters>\n{json.dumps(payload, ensure_ascii=False)}\n</clusters>"
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=PROPOSE_VIEWS_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        temperature=0.2,
    ))
    tagged = parse_tagged(resp.text or "")
    block = tagged.get("decisions", "")
    out: list[dict[str, Any]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("propose_views: skip malformed decision line %r: %s",
                        line[:120], exc)
            continue
        if isinstance(obj, dict) and obj.get("cluster_id"):
            out.append(obj)
    return out
