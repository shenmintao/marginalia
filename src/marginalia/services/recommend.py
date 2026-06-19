"""Recommendation / discovery service.

Goal: cut agent loop count.

When the agent has identified one relevant entry, the discovery layer
hands it the most likely neighbours immediately — no extra search /
read_files / re-rank loops. Two access patterns:

  - find_related(entry_id, top_k):  given a known-relevant seed entry,
                                    return entries most likely related
  - find_related_to_query(...):     reserved for future query-time use

Algorithm: random walk with restart over the entry_relations graph.

  edges = entry_relations rows (any source_kind)
  weight(a, b) = observation_count
  start at seed, K independent walks of bounded length. At each step:
    - with prob alpha (0.15) restart at seed
    - else jump to a random neighbour weighted by edge weight
  return entries by visit frequency (excluding the seed itself).

Why random walk over a single SQL "neighbours by weight" query:
  - propagates structural distance: a 2-hop neighbour through 3
    different paths gets credit
  - naturally combines signals (cooccurrence + tag_overlap +
    citation_graph) without picking one — they all become edges
  - tunable: alpha controls "stay close to seed" vs "wander far"
  - cheap: O(walks * length) lookups, all in-memory after one db pull

Limits:
  - graph cached per call (rebuild on each invocation; small enough)
  - top-k returned with scores (visit count / total walk steps)
  - if seed has no edges → empty result, not an error
"""
from __future__ import annotations

import logging
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_relations as relations_repo

log = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
DEFAULT_WALKS = 1000
DEFAULT_WALK_LENGTH = 6
DEFAULT_RESTART_ALPHA = 0.15


@dataclass(slots=True)
class RelatedEntry:
    """One row of find_related output."""
    entry_id: str
    display_name: str
    score: float           # visit-frequency, 0..1
    visit_count: int
    direct_edge_weight: int  # observation_count of direct seed→entry edge,
                             # 0 if there is no direct edge


async def find_related(
    db: AsyncSession,
    *,
    seed_entry_id: str,
    top_k: int = DEFAULT_TOP_K,
    walks: int = DEFAULT_WALKS,
    walk_length: int = DEFAULT_WALK_LENGTH,
    restart_alpha: float = DEFAULT_RESTART_ALPHA,
    rng_seed: int | None = None,
    include_unvetted: bool = False,
) -> list[RelatedEntry]:
    """Run RWR from `seed_entry_id`. Returns at most `top_k` non-seed
    entries ordered by visit frequency.

    By default this is a pure-read path: only edges with vetted=True
    participate and no relation-vetting task or LLM call is triggered.
    Set `include_unvetted=True` to walk over the raw mined graph without
    vetting; useful for debugging or for the `/discover --all` CLI flag.

    `rng_seed` is for tests; production uses system RNG."""
    edges = await _load_edges(db, include_unvetted=include_unvetted)
    if seed_entry_id not in edges or not edges[seed_entry_id]:
        return []

    rng = random.Random(rng_seed) if rng_seed is not None else random
    visits: defaultdict[str, int] = defaultdict(int)
    total_steps = 0
    for _ in range(walks):
        node = seed_entry_id
        for _ in range(walk_length):
            if rng.random() < restart_alpha:
                node = seed_entry_id
                continue
            neighbours = edges.get(node)
            if not neighbours:
                node = seed_entry_id
                continue
            node = _weighted_pick(neighbours, rng)
            if node == seed_entry_id:
                continue
            visits[node] += 1
            total_steps += 1
    if not visits or total_steps == 0:
        return []

    direct: dict[str, int] = {
        nb: w for nb, w in (edges.get(seed_entry_id) or [])
    }
    ranked = sorted(visits.items(), key=lambda kv: kv[1], reverse=True)
    top_ids = [eid for eid, _ in ranked[:top_k]]
    name_by_id = await _resolve_display_names(db, top_ids)
    out: list[RelatedEntry] = []
    for eid, count in ranked[:top_k]:
        out.append(RelatedEntry(
            entry_id=eid,
            display_name=name_by_id.get(eid, ""),
            score=count / total_steps,
            visit_count=count,
            direct_edge_weight=direct.get(eid, 0),
        ))
    return out


async def _load_edges(
    db: AsyncSession,
    *,
    include_unvetted: bool = False,
) -> dict[str, list[tuple[str, int]]]:
    """Load entry_relations as adjacency list. Symmetric pairs: each
    relation is materialised as two edges so walks can go either way.

    Filters:
      - live entries only on side A (joined directly).
      - live entries on side B via a second filter pass.
      - vetted=True only, unless include_unvetted is set. Without
        gating, statistical noise from the miners (catch-all tags,
        passing references) shows up in recommendations.
    """
    rows = await relations_repo.list_edges_with_live_a(
        db, vetted_only=not include_unvetted,
    )
    if not rows:
        return {}

    # Filter the b side too. We do a second query rather than a self-
    # join with two filters because it's clearer and SQLite's planner
    # can't always optimise the double-FK case.
    b_ids = {b for _, b, _ in rows}
    live_b = await entries_repo.filter_live_ids(db, list(b_ids))
    live_b_set = set(live_b)

    edges: defaultdict[str, list[tuple[str, int]]] = defaultdict(list)
    for a, b, weight in rows:
        if b not in live_b_set:
            continue
        w = max(1, int(weight or 1))
        edges[a].append((b, w))
        edges[b].append((a, w))
    return dict(edges)


async def _resolve_display_names(
    db: AsyncSession, entry_ids: Iterable[str],
) -> dict[str, str]:
    return await entries_repo.list_display_names(db, list(entry_ids))


def _weighted_pick(
    neighbours: list[tuple[str, int]], rng: random.Random,
) -> str:
    if len(neighbours) == 1:
        return neighbours[0][0]
    total = sum(w for _, w in neighbours)
    target = rng.randint(1, total)
    running = 0
    for node, w in neighbours:
        running += w
        if running >= target:
            return node
    return neighbours[-1][0]
