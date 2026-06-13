"""Allowed values for string-enum columns.

Single source of truth shared between SQLAlchemy models (CheckConstraint) and
Alembic migrations. Adding a new value: append here, write a migration that
ALTERs the CHECK constraint, and only then start writing the new value.
"""
from __future__ import annotations


TASK_STATUSES: tuple[str, ...] = ("pending", "running", "done", "dead")

INGEST_STATUSES: tuple[str, ...] = ("pending", "processing", "done", "failed")

ENTRY_LIFECYCLES: tuple[str, ...] = (
    "active",
    "demoted",
    "archived",
    "manual_active",
    "manual_archived",
)

FILE_KINDS: tuple[str, ...] = (
    "text", "table", "log", "image", "audio", "video", "code", "container",
)

TAG_FACETS: tuple[str, ...] = (
    "topic", "form", "time", "source", "language", "extra",
)

ENTRY_TAG_SOURCES: tuple[str, ...] = ("ingest", "dedup_seed", "enrich_tags")

SESSION_END_REASONS: tuple[str, ...] = ("cleared", "normal", "unclean", "deleted")

JOURNAL_SOURCE_KINDS: tuple[str, ...] = ("reflect_turn", "insight")

ENTRY_RELATION_SOURCE_KINDS: tuple[str, ...] = (
    "mine_citation_graph",
    "mine_corpus_evidence",
    "mine_session_cooccurrence",
    "mine_tag_overlap",
)
# Historical note: the column once carried a server-side default of
# "reflect" (relations written during the reflect phase). All such writes
# have been delegated to offline miners, so "reflect" is no longer a legal
# value. "mine_corpus_evidence" is retained for historical rows from the
# retired LLM-gated miner.


def _in_clause(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"
