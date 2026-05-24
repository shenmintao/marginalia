"""file_entry user-side service — DESIGN.md §14.1.

Operations:
  - rename_entry         change display_name (with on_conflict policy)
  - move_entry           change folder_id
  - change_lifecycle     user-only lifecycle transitions
  - soft_delete_entry    set deleted_at + purge_after

User write boundary (design §14.1):
  - User may write folder_id / display_name / lifecycle / deleted_at /
    purge_after on file_entries
  - User MUST NOT write catalog_id / extra / entry_tags — those are AI fields
  - lifecycle: user can only transition to/from manual_active and
    manual_archived, plus restoring archived/demoted entries to active.
    The demoted/archived auto-states are produced by suggest_*; user
    overriding them goes through the manual_* sentinel states.

Audit: every change writes one audit_event so the human-side log shows
what the user did.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry, Folder
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.services.upload import (
    DEFAULT_ON_CONFLICT,
    DisplayNameConflictError,
    _existing_entry_with_name,
    _resolve_display_name,
)
from marginalia.storage import MirrorStorage, get_storage

_USER_LIFECYCLE_TARGETS = {
    "active", "manual_active", "manual_archived",
}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class EntryNotFoundError(Exception):
    pass

class InvalidLifecycleTransitionError(Exception):
    pass

async def _get_live_entry(db: AsyncSession, entry_id: str) -> FileEntry:
    e = await db.get(FileEntry, entry_id)
    if e is None or e.deleted_at is not None:
        raise EntryNotFoundError(entry_id)
    return e

async def _mirror_sync_disk_path(
    db: AsyncSession, entry: FileEntry, *, reason: str,
) -> None:
    """If storage is mirror, move the on-disk file to match the entry's
    new (folder, display_name). No-op for local + s3 backends.

    The entry must already have its new folder_id / display_name set.
    On any failure we raise — caller's session rollback un-does the
    db change so disk and db stay consistent.
    """
    storage = get_storage()
    if not isinstance(storage, MirrorStorage):
        return
    file_row = await db.get(File, entry.file_id)
    if file_row is None or file_row.deleted_at is not None:
        return
    folder_path = await _build_folder_display_path(db, entry.folder_id)
    new_rel = (
        f"{folder_path}/{entry.display_name}".lstrip("/")
        if folder_path else entry.display_name
    )
    new_key = await storage.rename(file_row.storage_key, new_rel)
    if new_key != file_row.storage_key:
        file_row.storage_key = new_key
        file_row.updated_at = _utcnow()

async def _build_folder_display_path(
    db: AsyncSession, folder_id: str | None,
) -> str:
    """Walk Folder.parent_id to build '/research/llm' style display path.
    Empty string for the root folder (folder_id None or root sentinel)."""
    if folder_id is None:
        return ""
    parts: list[str] = []
    cur_id: str | None = folder_id
    seen: set[str] = set()
    while cur_id and cur_id not in seen:
        seen.add(cur_id)
        f = await db.get(Folder, cur_id)
        if f is None or f.parent_id is None:
            # root folder is sentinel; skip its name (typically empty)
            if f is not None and f.name:
                parts.append(f.name)
            break
        parts.append(f.name)
        cur_id = f.parent_id
    return "/" + "/".join(reversed(parts)) if parts else ""

async def rename_entry(
    db: AsyncSession,
    *,
    entry_id: str,
    new_name: str,
    on_conflict: Literal["rename", "error", "skip"] = DEFAULT_ON_CONFLICT,
) -> FileEntry:
    entry = await _get_live_entry(db, entry_id)
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("display_name cannot be empty")
    if new_name == entry.display_name:
        return entry

    if on_conflict == "error":
        clash = await _existing_entry_with_name(db, entry.folder_id, new_name)
        if clash is not None and clash.id != entry.id:
            raise DisplayNameConflictError(
                folder_id=entry.folder_id,
                display_name=new_name,
                existing_entry_id=clash.id,
                existing_file_id=clash.file_id,
            )
        final = new_name
        auto_renamed = False
    elif on_conflict == "skip":
        clash = await _existing_entry_with_name(db, entry.folder_id, new_name)
        if clash is not None and clash.id != entry.id:
            return entry  # silently no-op
        final = new_name
        auto_renamed = False
    else:  # rename
        final, auto_renamed = await _resolve_display_name(
            db, entry.folder_id, new_name
        )

    old_name = entry.display_name
    entry.display_name = final
    entry.updated_at = _utcnow()
    await _mirror_sync_disk_path(db, entry, reason="rename")
    await audit_events_repo.append(db, kind="entry_renamed", payload={
        "entry_id": entry.id,
        "folder_id": entry.folder_id,
        "old_name": old_name,
        "new_name": final,
        "auto_renamed": auto_renamed,
    })
    return entry

async def move_entry(
    db: AsyncSession,
    *,
    entry_id: str,
    new_folder_id: str,
    on_conflict: Literal["rename", "error", "skip"] = DEFAULT_ON_CONFLICT,
) -> FileEntry:
    entry = await _get_live_entry(db, entry_id)
    if entry.folder_id == new_folder_id:
        return entry

    target = await db.get(Folder, new_folder_id)
    if target is None or target.deleted_at is not None:
        raise ValueError(f"target folder not found: {new_folder_id}")

    desired = entry.display_name
    if on_conflict == "error":
        clash = await _existing_entry_with_name(db, new_folder_id, desired)
        if clash is not None:
            raise DisplayNameConflictError(
                folder_id=new_folder_id,
                display_name=desired,
                existing_entry_id=clash.id,
                existing_file_id=clash.file_id,
            )
        final = desired
        auto_renamed = False
    elif on_conflict == "skip":
        clash = await _existing_entry_with_name(db, new_folder_id, desired)
        if clash is not None:
            return entry
        final = desired
        auto_renamed = False
    else:
        final, auto_renamed = await _resolve_display_name(
            db, new_folder_id, desired
        )

    old_folder = entry.folder_id
    entry.folder_id = new_folder_id
    entry.display_name = final
    entry.updated_at = _utcnow()
    await _mirror_sync_disk_path(db, entry, reason="move")
    await audit_events_repo.append(db, kind="entry_moved", payload={
        "entry_id": entry.id,
        "old_folder_id": old_folder,
        "new_folder_id": new_folder_id,
        "display_name": final,
        "auto_renamed": auto_renamed,
    })
    return entry

async def change_lifecycle(
    db: AsyncSession,
    *,
    entry_id: str,
    new_lifecycle: str,
) -> FileEntry:
    entry = await _get_live_entry(db, entry_id)
    if new_lifecycle not in _USER_LIFECYCLE_TARGETS:
        raise InvalidLifecycleTransitionError(
            f"user may only set lifecycle to one of {_USER_LIFECYCLE_TARGETS}; "
            f"automatic states (demoted/archived) are produced by background tasks."
        )
    if entry.lifecycle == new_lifecycle:
        return entry
    old = entry.lifecycle
    entry.lifecycle = new_lifecycle
    entry.updated_at = _utcnow()
    await audit_events_repo.append(db, kind="lifecycle_changed", payload={
        "entry_id": entry.id,
        "old": old,
        "new": new_lifecycle,
        "trigger": "user",
    })
    return entry

async def soft_delete_entry(
    db: AsyncSession,
    *,
    entry_id: str,
    purge_after_seconds: int = 7 * 86400,
) -> FileEntry:
    entry = await _get_live_entry(db, entry_id)
    now = _utcnow()
    entry.deleted_at = now
    entry.purge_after = now + timedelta(seconds=max(0, purge_after_seconds))
    entry.updated_at = now
    await audit_events_repo.append(db, kind="entry_soft_deleted", payload={
        "entry_id": entry.id,
        "folder_id": entry.folder_id,
        "display_name": entry.display_name,
        "purge_after": entry.purge_after.isoformat(),
    })
    return entry
