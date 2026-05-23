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


# 11 kinds: 10 business + 1 dispatcher (design.md §9.1).
# Adding a new kind = registering a handler; this list is informational.

# Online (user is waiting) ----------------------------------------------------
KIND_REFLECT_TURN = "reflect_turn"
KIND_INGEST_FILE = "ingest_file"

# Self-healing ----------------------------------------------------------------
KIND_RECOVER_STUCK_TASKS = "recover_stuck_tasks"

# Honor user intent -----------------------------------------------------------
KIND_PURGE_DELETED_FILES = "purge_deleted_files"

# Quality foundation ----------------------------------------------------------
KIND_NORMALIZE_TAGS = "normalize_tags"
KIND_ENRICH_TAGS = "enrich_tags"

# Structural evolution --------------------------------------------------------
KIND_RESTRUCTURE_CATALOGS = "restructure_catalogs"

# Lifecycle judgements --------------------------------------------------------
KIND_SUGGEST_DEMOTION = "suggest_demotion"
KIND_SUGGEST_ARCHIVAL = "suggest_archival"

# Mining (corpus-side discovery, see design.md §9.x) -------------------------
# Pure stats / structural sampling that produce entry_relations / views.
KIND_MINE_SESSION_COOCCURRENCE = "mine_session_cooccurrence"
KIND_MINE_CORPUS_EVIDENCE = "mine_corpus_evidence"
KIND_PROPOSE_VIEWS = "propose_views"
KIND_REFRESH_ENTRY_EXTRA = "refresh_entry_extra"

# Audit retention -------------------------------------------------------------
KIND_PRUNE_AUDIT_EVENTS = "prune_audit_events"
KIND_PRUNE_TASK_OUTCOMES = "prune_task_outcomes"

# Dispatcher ------------------------------------------------------------------
KIND_PERIODIC_TICK = "periodic_tick"


# Priorities: smaller = higher. Layers reflect Marginalia's value ordering:
#   30 / 50    online (user is waiting)
#   100        self-healing (system mustn't get stuck)
#   150        honor user intent (deletion lifecycle)
#   200 / 215  quality foundation (normalize must precede enrich)
#   220        structural evolution (catalogs depend on stable tags)
#   240 / 250  lifecycle judgements (demote/archive depend on structural stability)
#   260        audit retention
#   300        dispatcher (lowest — never starves real work)
DEFAULT_PRIORITIES: Mapping[str, int] = {
    KIND_REFLECT_TURN: 30,
    KIND_INGEST_FILE: 50,
    KIND_RECOVER_STUCK_TASKS: 100,
    KIND_PURGE_DELETED_FILES: 150,
    KIND_NORMALIZE_TAGS: 200,
    KIND_ENRICH_TAGS: 215,
    KIND_RESTRUCTURE_CATALOGS: 220,
    KIND_SUGGEST_DEMOTION: 240,
    KIND_SUGGEST_ARCHIVAL: 250,
    KIND_MINE_SESSION_COOCCURRENCE: 245,
    KIND_MINE_CORPUS_EVIDENCE: 248,
    KIND_PROPOSE_VIEWS: 252,
    KIND_REFRESH_ENTRY_EXTRA: 255,
    KIND_PRUNE_AUDIT_EVENTS: 260,
    KIND_PRUNE_TASK_OUTCOMES: 265,
    KIND_PERIODIC_TICK: 300,
}


# Periodic kinds and their re-enqueue intervals (design.md §9.3).
# `periodic_tick` itself runs every 10 minutes (handled separately, not listed
# here — it cannot dispatch itself). For each kind below, the dispatcher checks
# "when did the last `done` row finish?"; if older than the interval, enqueue.
PERIODIC_INTERVALS: Mapping[str, timedelta] = {
    KIND_RECOVER_STUCK_TASKS: timedelta(minutes=10),
    KIND_PURGE_DELETED_FILES: timedelta(days=1),
    KIND_NORMALIZE_TAGS: timedelta(hours=6),
    KIND_ENRICH_TAGS: timedelta(days=5),
    KIND_RESTRUCTURE_CATALOGS: timedelta(days=7),
    KIND_SUGGEST_DEMOTION: timedelta(days=7),
    KIND_SUGGEST_ARCHIVAL: timedelta(days=14),
    KIND_MINE_SESSION_COOCCURRENCE: timedelta(days=1),
    KIND_MINE_CORPUS_EVIDENCE: timedelta(days=7),
    KIND_PROPOSE_VIEWS: timedelta(days=14),
    KIND_REFRESH_ENTRY_EXTRA: timedelta(days=7),
    KIND_PRUNE_AUDIT_EVENTS: timedelta(days=1),
    KIND_PRUNE_TASK_OUTCOMES: timedelta(days=7),
}
