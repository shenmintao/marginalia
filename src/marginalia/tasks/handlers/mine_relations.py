"""mine_relations - unified cheap-signal miner dispatcher.

Three non-LLM miners share a daily slot. They write raw observations into
entry_relations rows that explicit `/discover --vet` requests or optional
batch vet_relations later judge:

  miner                      legacy interval
  ------------------------------------------
  session_cooccurrence       daily
  tag_overlap                daily
  citation_graph             daily

Why merged: dispatcher bookkeeping was more complex without buying anything
(the three miners do not constrain each other, but they are always
co-scheduled). One kind, one interval, three phases.

Payload (all optional):
  {"miners": ["session_cooccurrence", "tag_overlap",
              "citation_graph"]}  # default: all
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from marginalia.tasks.handlers.mine_citation_graph import handle_mine_citation_graph
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
)

_MINERS = {
    "session_cooccurrence": handle_mine_session_cooccurrence,
    "tag_overlap": handle_mine_tag_overlap,
    "citation_graph": handle_mine_citation_graph,
}


@task_handler(KIND_MINE_RELATIONS)
async def handle_mine_relations(payload: Mapping[str, Any]) -> None:
    miners = list(payload.get("miners") or DEFAULT_MINERS)
    for name in miners:
        fn = _MINERS.get(name)
        if fn is None:
            log.warning("mine_relations: unknown miner %r - skipped", name)
            continue
        sub_payload = payload.get(name) or {}
        try:
            await fn(sub_payload)
        except Exception:
            log.exception("mine_relations: miner %s raised - continuing", name)
