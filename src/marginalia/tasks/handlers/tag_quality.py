"""tag_quality — unified tag-vocabulary maintenance.

DESIGN.md §9.1 + §9.4 + §14.4.

Two LLM-driven phases that always run in this order:
  1. normalize_tags — synonym/case/spelling merges (must precede enrich)
  2. enrich_tags    — describe sparse tags so they become useful

Why merged: normalize MUST run before enrich (enriching synonyms is wasted
work and creates inconsistent descriptions). They were already two
back-to-back periodics with the same target table; one kind expresses the
ordering naturally and halves the dispatcher bookkeeping.

Each phase still records its own task_outcomes row keyed by its legacy
task_kind ("normalize_tags" / "enrich_tags") — analytics that group by
phase keep working, and the enrich phase's internal MIN_INTERVAL check
(see [[enrich-min-interval]]) self-throttles even when the unified kind
fires daily.

Payload (all optional):
  {"phases": ["normalize", "enrich"]}   # default both, this order
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from marginalia.tasks.handlers.enrich_tags import handle_enrich_tags
from marginalia.tasks.handlers.normalize_tags import handle_normalize_tags
from marginalia.tasks.kinds import KIND_TAG_QUALITY, task_handler

log = logging.getLogger(__name__)


@task_handler(KIND_TAG_QUALITY)
async def handle_tag_quality(payload: Mapping[str, Any]) -> None:
    phases = list(payload.get("phases") or ["normalize", "enrich"])
    for phase in phases:
        if phase == "normalize":
            await handle_normalize_tags(payload.get("normalize") or {})
        elif phase == "enrich":
            await handle_enrich_tags(payload.get("enrich") or {})
        else:
            log.warning("tag_quality: unknown phase %r — skipped", phase)
