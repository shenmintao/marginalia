"""SQLAlchemy ORM models for Marginalia.

14 business tables organized by the four-layer architecture (design.md §7):

User-visible (3):
  - folders, file_entries, files

Audit (3):
  - audit_events, sessions, conversations

AI-internal (7):
  - catalogs, views, tags, tag_aliases, entry_tags
  - entry_relations, journal

Infrastructure (1):
  - tasks

Importing this package registers every table on Base.metadata so Alembic
autogenerate / Base.metadata.create_all picks them all up.
"""

from marginalia.db.models.base import Base
from marginalia.db.models.user_visible import File, FileEntry, Folder
from marginalia.db.models.audit import AuditEvent, Conversation, Session
from marginalia.db.models.ai_structural import (
    Catalog,
    EntryTag,
    Tag,
    TagAlias,
    View,
)
from marginalia.db.models.ai_recall import EntryRelation, Journal
from marginalia.db.models.task_outcomes import TaskOutcome
from marginalia.db.models.tasks import Task

__all__ = [
    "AuditEvent",
    "Base",
    "Catalog",
    "Conversation",
    "EntryRelation",
    "EntryTag",
    "File",
    "FileEntry",
    "Folder",
    "Journal",
    "Session",
    "Tag",
    "TagAlias",
    "Task",
    "TaskOutcome",
    "View",
]
