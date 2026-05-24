"""mine_relations — unified miner dispatcher (design.md §9.x).

Four miners share a daily slot. They write into entry_relations rows that
vet_relations later judges:

  miner                      legacy interval     throttle
  ------------------------------------------------------------
  session_cooccurrence       daily               (per-tick)
  tag_overlap                daily               (per-tick)
  citation_graph             daily               (per-tick)
  corpus_evidence            weekly              MIN_INTERVAL inside

corpus_evidence checks its own task_outcomes recency and noops if it ran
within the last week — so this kind firing daily is harmless.

Why merged: dispatcher bookkeeping was 4× more complex without buying
anything (the four miners don't constrain each other, but they're always
co-scheduled). One kind, one interval, four phases.

Payload (all optional):
  {"miners": ["session_cooccurrence", "tag_overlap",
              "citation_graph", "corpus_evidence"]}  # default: all
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from marginalia.tasks.handlers.mine_citation_graph import handle_mine_citation_graph
from marginalia.tasks.handlers.mine_corpus_evidence import handle_mine_corpus_evidence
from marginalia.tasks.handlers.mine_session_cooccurrence import (
    handle_mine_session_cooccurrence,
)
from marginalia.tasks.handlers.mine_tag_overlap import handle_mine_tag_overlap
from marginalia.tasks.kinds import KIND_MINE_RELATIONS, task_handler

log = logging.getLogger(__name__)

DEFAULT_MINERS = (
    "session_cooccurrence",
    "tag_overlap",
    "citation_graph",
    "corpus_evidence",
)

_MINERS = {
    "session_cooccurrence": handle_mine_session_cooccurrence,
    "tag_overlap": handle_mine_tag_overlap,
    "citation_graph": handle_mine_citation_graph,
    "corpus_evidence": handle_mine_corpus_evidence,
}


@task_handler(KIND_MINE_RELATIONS)
async def handle_mine_relations(payload: Mapping[str, Any]) -> None:
    miners = list(payload.get("miners") or DEFAULT_MINERS)
    for name in miners:
        fn = _MINERS.get(name)
        if fn is None:
            log.warning("mine_relations: unknown miner %r — skipped", name)
            continue
        sub_payload = payload.get(name) or {}
        try:
            await fn(sub_payload)
        except Exception:
            log.exception("mine_relations: miner %s raised — continuing", name)
