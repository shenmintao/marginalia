"""Unified async task queue (no external broker)."""

from marginalia.tasks.kinds import (
    DEFAULT_PRIORITIES,
    KIND_INGEST_FILE,
    KIND_MINE_RELATIONS,
    KIND_PERIODIC_TICK,
    KIND_PROPOSE_VIEWS,
    KIND_PRUNE,
    KIND_PURGE_DELETED_FILES,
    KIND_RECOVER_STUCK_TASKS,
    KIND_REFLECT_TURN,
    KIND_REFRESH_ENTRY_EXTRA,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_SUGGEST_LIFECYCLE,
    KIND_SUMMARIZE_SESSION,
    KIND_TAG_QUALITY,
    KIND_VET_RELATIONS,
    PERIODIC_INTERVALS,
    get_handler,
    registered_kinds,
    task_handler,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.runner import TaskRunner

__all__ = [
    "DEFAULT_PRIORITIES",
    "KIND_INGEST_FILE",
    "KIND_MINE_RELATIONS",
    "KIND_PERIODIC_TICK",
    "KIND_PROPOSE_VIEWS",
    "KIND_PRUNE",
    "KIND_PURGE_DELETED_FILES",
    "KIND_RECOVER_STUCK_TASKS",
    "KIND_REFLECT_TURN",
    "KIND_REFRESH_ENTRY_EXTRA",
    "KIND_RESTRUCTURE_CATALOGS",
    "KIND_SUGGEST_LIFECYCLE",
    "KIND_SUMMARIZE_SESSION",
    "KIND_TAG_QUALITY",
    "KIND_VET_RELATIONS",
    "PERIODIC_INTERVALS",
    "TaskRunner",
    "enqueue",
    "get_handler",
    "registered_kinds",
    "task_handler",
]
