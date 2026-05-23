"""Unified async task queue (no external broker)."""

from marginalia.tasks.kinds import (
    DEFAULT_PRIORITIES,
    KIND_ENRICH_TAGS,
    KIND_INGEST_FILE,
    KIND_NORMALIZE_TAGS,
    KIND_PERIODIC_TICK,
    KIND_PRUNE_AUDIT_EVENTS,
    KIND_PRUNE_TASK_OUTCOMES,
    KIND_PURGE_DELETED_FILES,
    KIND_RECOVER_STUCK_TASKS,
    KIND_REFLECT_TURN,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_SUGGEST_ARCHIVAL,
    KIND_SUGGEST_DEMOTION,
    PERIODIC_INTERVALS,
    get_handler,
    registered_kinds,
    task_handler,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.runner import TaskRunner

__all__ = [
    "DEFAULT_PRIORITIES",
    "KIND_ENRICH_TAGS",
    "KIND_INGEST_FILE",
    "KIND_NORMALIZE_TAGS",
    "KIND_PERIODIC_TICK",
    "KIND_PRUNE_AUDIT_EVENTS",
    "KIND_PRUNE_TASK_OUTCOMES",
    "KIND_PURGE_DELETED_FILES",
    "KIND_RECOVER_STUCK_TASKS",
    "KIND_REFLECT_TURN",
    "KIND_RESTRUCTURE_CATALOGS",
    "KIND_SUGGEST_ARCHIVAL",
    "KIND_SUGGEST_DEMOTION",
    "PERIODIC_INTERVALS",
    "TaskRunner",
    "enqueue",
    "get_handler",
    "registered_kinds",
    "task_handler",
]
