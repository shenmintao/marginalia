from __future__ import annotations

from datetime import timedelta
from typing import Awaitable, Callable, Mapping, MutableMapping

TaskHandler = Callable[[Mapping[str, object]], Awaitable[None]]

_REGISTRY: MutableMapping[str, TaskHandler] = {}


def task_handler(kind: str) -> Callable[[TaskHandler], TaskHandler]:
    """Register an async handler for a task kind."""

    def decorator(fn: TaskHandler) -> TaskHandler:
        if kind in _REGISTRY:
            raise RuntimeError(f"Task handler for {kind!r} already registered")
        _REGISTRY[kind] = fn
        return fn

    return decorator


def get_handler(kind: str) -> TaskHandler | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


# 14 kinds: 13 business + 1 dispatcher (DESIGN.md §9.1).
# Adding a new kind = registering a handler; this list is informational.

# Online (user is waiting) ----------------------------------------------------
KIND_REFLECT_TURN = "reflect_turn"
KIND_INGEST_FILE = "ingest_file"

# Cross-session synthesis -----------------------------------------------------
KIND_SUMMARIZE_SESSION = "summarize_session"

# Self-healing ----------------------------------------------------------------
KIND_RECOVER_STUCK_TASKS = "recover_stuck_tasks"

# Honor user intent -----------------------------------------------------------
KIND_PURGE_DELETED_FILES = "purge_deleted_files"

# Quality foundation (normalize then enrich, in one kind) --------------------
KIND_TAG_QUALITY = "tag_quality"

# Structural evolution --------------------------------------------------------
KIND_RESTRUCTURE_CATALOGS = "restructure_catalogs"

# Lifecycle judgements (active→demoted→archived in one kind) -----------------
KIND_SUGGEST_LIFECYCLE = "suggest_lifecycle"

# Mining (4 miners → entry_relations) ----------------------------------------
KIND_MINE_RELATIONS = "mine_relations"
KIND_VET_RELATIONS = "vet_relations"
KIND_PROPOSE_VIEWS = "propose_views"
KIND_REFRESH_ENTRY_EXTRA = "refresh_entry_extra"

# Audit retention (audit_events + task_outcomes in one kind) -----------------
KIND_PRUNE = "prune"

# Dispatcher ------------------------------------------------------------------
KIND_PERIODIC_TICK = "periodic_tick"


# Priorities: smaller = higher. Layers reflect Marginalia's value ordering:
#   30 / 50 / 60   online (user is waiting)
#   100            self-healing (system mustn't get stuck)
#   150            honor user intent (deletion lifecycle)
#   200            quality foundation
#   220            structural evolution (catalogs depend on stable tags)
#   240            lifecycle judgements
#   245 / 251 / 252 / 255   mining family
#   260            audit retention
#   300            dispatcher (lowest — never starves real work)
DEFAULT_PRIORITIES: Mapping[str, int] = {
    KIND_REFLECT_TURN: 30,
    KIND_INGEST_FILE: 50,
    KIND_SUMMARIZE_SESSION: 60,
    KIND_RECOVER_STUCK_TASKS: 100,
    KIND_PURGE_DELETED_FILES: 150,
    KIND_TAG_QUALITY: 200,
    KIND_RESTRUCTURE_CATALOGS: 220,
    KIND_SUGGEST_LIFECYCLE: 240,
    KIND_MINE_RELATIONS: 245,
    KIND_VET_RELATIONS: 251,
    KIND_PROPOSE_VIEWS: 252,
    KIND_REFRESH_ENTRY_EXTRA: 255,
    KIND_PRUNE: 260,
    KIND_PERIODIC_TICK: 300,
}


# Periodic kinds and their re-enqueue intervals (DESIGN.md §9.3).
# `periodic_tick` itself is not listed (it self-schedules every 10 min).
# `summarize_session` is also not listed: it's per-session, dispatched in
# periodic_tick._dispatch_summarize_sessions with dedup_key=f"...:{sid}".
#
# Where two former kinds were merged, the kept interval is whichever was
# already shorter (the unified handler self-throttles longer phases via
# task_outcomes recency lookups inside the merged handler):
#   tag_quality       = min(normalize 6h, enrich 5d)        → 6h
#   suggest_lifecycle = min(demote 7d, archive 14d)         → 7d
#   mine_relations    = min(per-miner 1d, evidence 7d)      → 1d
#   prune             = min(audit 1d, outcomes 7d)          → 1d
PERIODIC_INTERVALS: Mapping[str, timedelta] = {
    KIND_RECOVER_STUCK_TASKS: timedelta(minutes=10),
    KIND_PURGE_DELETED_FILES: timedelta(days=1),
    KIND_TAG_QUALITY: timedelta(hours=6),
    KIND_RESTRUCTURE_CATALOGS: timedelta(days=7),
    KIND_SUGGEST_LIFECYCLE: timedelta(days=7),
    KIND_MINE_RELATIONS: timedelta(days=1),
    KIND_VET_RELATIONS: timedelta(days=1),
    KIND_PROPOSE_VIEWS: timedelta(days=14),
    KIND_REFRESH_ENTRY_EXTRA: timedelta(days=7),
    KIND_PRUNE: timedelta(days=1),
}


# Kinds whose handler will hit an LLM endpoint on its first step. The
# runner consults this set so it can fail-fast (with a clear message)
# instead of letting handlers crash with `OpenAIError: Missing
# credentials` when no api_key is configured.
#
# Update when adding/removing a handler that calls get_chat_client /
# get_completion_client. Handlers not in this set must be safe to run
# without any LLM credentials at all.
LLM_DEPENDENT_KINDS: frozenset[str] = frozenset({
    KIND_INGEST_FILE,
    KIND_REFLECT_TURN,
    KIND_SUMMARIZE_SESSION,
    KIND_TAG_QUALITY,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_VET_RELATIONS,
    KIND_PROPOSE_VIEWS,
    KIND_REFRESH_ENTRY_EXTRA,
    # mine_relations dispatches mine_corpus_evidence which calls LLM.
    KIND_MINE_RELATIONS,
})
