"""Upload service: streaming sha256 + dedup + auto folder + name-conflict policy.

Implements DESIGN.md §12.1 end-to-end. The route handler hands us:
  - an async byte stream of the user's bytes
  - a fallback display_name (the local basename, used if remote path didn't
    specify one)
  - a `<remote>` path string
  - an `on_conflict` policy chosen by the slash-command client

We:
  1. split <remote> into folder segments + optional explicit display_name
  2. walk / auto-create the folder chain (services.folders)
  3. apply on_conflict policy if the desired display_name is already taken in
     the destination folder (rename | error | skip — see _NameConflictPolicy)
  4. stream bytes through StreamHasher into storage at a tentative key
  5. SELECT files WHERE sha256 = <hash>:
     * hit  → drop the temp object, find a seed entry (any file_entry sharing
              file_id), INSERT a new entry copying catalog_id / extra +
              entry_tags rows (source='dedup_seed'), do NOT enqueue ingest
     * miss → INSERT files row (description fields blank, ingest_status=
              'pending'), INSERT entry (AI fields blank), enqueue ingest_file

Every state change emits an audit_event in the same transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryTag, File, FileEntry
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import files as files_repo
from marginalia.services.folders import (
    AmbiguousRemotePathError,
    parse_remote_folder,
    resolve_or_create_folder,
    split_remote_path,
)
from marginalia.storage.base import StorageBackend
from marginalia.storage.mirror import MirrorStorage
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.utils.hashing import StreamHasher
from marginalia.utils.ids import new_id, storage_prefix

_NameConflictPolicy = Literal["rename", "error", "skip"]

def _resolve_default_on_conflict() -> _NameConflictPolicy:
    # Resolved once at module import; env-driven via DEFAULT_ON_CONFLICT
    # in .env. Per-call overrides on /v1/upload and file-entry routes
    # still win when set.
    from marginalia.config import get_settings
    return get_settings().default_on_conflict

DEFAULT_ON_CONFLICT: _NameConflictPolicy = _resolve_default_on_conflict()

@dataclass(slots=True)
class UploadResult:
    file_id: str
    entry_id: str
    folder_id: str | None
    display_name: str
    deduped: bool          # True if sha256 hit an existing file
    auto_renamed: bool     # True if display_name was suffixed (rename policy)
    skipped: bool = False  # True if skip policy returned a pre-existing entry

class DisplayNameConflictError(Exception):
    """Raised when on_conflict='error' and the target name is taken.

    Carries enough context for the route handler to translate into HTTP 409.
    """

    def __init__(
        self,
        *,
        folder_id: str | None,
        display_name: str,
        existing_entry_id: str,
        existing_file_id: str,
    ) -> None:
        super().__init__(
            f"display_name {display_name!r} already exists in folder {folder_id!r}"
        )
        self.folder_id = folder_id
        self.display_name = display_name
        self.existing_entry_id = existing_entry_id
        self.existing_file_id = existing_file_id

def _make_storage_key(file_id: str) -> str:
    top, sub = storage_prefix(file_id)
    return f"{top}/{sub}/{file_id}"

def _split_extension(name: str) -> tuple[str, str]:
    """Split into (stem, ext_with_dot). 'a.tar.gz' → ('a.tar', '.gz')."""
    stem, dot, ext = name.rpartition(".")
    if dot == "" or stem == "":
        return name, ""
    return stem, f".{ext}"

async def _existing_entry_with_name(
    session: AsyncSession, folder_id: str | None, name: str
) -> FileEntry | None:
    return await entries_repo.find_live_by_folder_and_name(
        session, folder_id, name,
    )

async def _resolve_display_name(
    session: AsyncSession, folder_id: str | None, desired: str
) -> tuple[str, bool]:
    """For policy=rename: find a free name, suffixing ' (N)' if needed."""
    stem, ext = _split_extension(desired)
    candidate = desired
    n = 0
    while True:
        if (await _existing_entry_with_name(session, folder_id, candidate)) is None:
            return candidate, n > 0
        n += 1
        candidate = f"{stem} ({n}){ext}"

async def upload(
    session: AsyncSession,
    storage: StorageBackend,
    *,
    stream: AsyncIterator[bytes],
    fallback_name: str,
    remote_path: str,
    display_name: str | None = None,
    content_type: str | None = None,
    on_conflict: _NameConflictPolicy = DEFAULT_ON_CONFLICT,
) -> UploadResult:
    """Upload a single file. See split_remote_path for path-resolution rules.

    `display_name` (when given) overrides any name derived from `remote_path`.
    The four legal combinations:
      - file → file        (`/a/b/foo.pdf`, no display_name)        → display = foo.pdf
      - file → folder/     (`/a/b/`, no display_name)               → display = local basename
      - file → folder      (`/a/b`, display_name="foo.pdf")         → display = foo.pdf
      - file → folder/     (`/a/b/`, display_name="x.pdf")          → display = x.pdf (override)
    The fifth combination (`/a/b` without display_name and no extension on
    the last segment) raises AmbiguousRemotePathError — the caller must
    disambiguate.
    """
    folder_segments, derived_name = split_remote_path(
        remote_path, display_name_override=display_name,
    )
    folder = await resolve_or_create_folder(session, folder_segments)
    folder_id = folder.id if folder is not None else None
    desired_name = (derived_name or fallback_name).strip()
    if not desired_name:
        raise ValueError("display_name and fallback_name both empty")

    # --- early conflict check (skip / error short-circuit before reading bytes)
    if on_conflict in ("error", "skip"):
        clash = await _existing_entry_with_name(session, folder_id, desired_name)
        if clash is not None:
            if on_conflict == "error":
                raise DisplayNameConflictError(
                    folder_id=folder_id,
                    display_name=desired_name,
                    existing_entry_id=clash.id,
                    existing_file_id=clash.file_id,
                )
            return UploadResult(
                file_id=clash.file_id,
                entry_id=clash.id,
                folder_id=folder_id,
                display_name=desired_name,
                deduped=False,
                auto_renamed=False,
                skipped=True,
            )

    # --- stream → storage at a tentative key (we don't yet know if dedup hits)
    tentative_file_id = new_id()
    tentative_storage_key = _make_storage_key(tentative_file_id)
    folder_display_path = (
        "/" + "/".join(folder_segments) if folder_segments else None
    )
    hasher = StreamHasher(stream)
    storage_key = await storage.put(
        tentative_storage_key, hasher.__aiter__(),
        content_type=content_type,
        display_name=desired_name,
        folder_path=folder_display_path,
    )
    sha256 = hasher.hexdigest
    size = hasher.size

    now = datetime.now(timezone.utc)

    # In mirror mode each upload gets its own file row — dedup is OFF
    # because the user explicitly opted into "files I can see in Finder
    # are the files I have". Sharing a file row across two folders
    # would require either symlinks (cross-platform pain) or a single
    # canonical disk path (which contradicts the mirror promise).
    is_mirror = isinstance(storage, MirrorStorage)

    if not is_mirror:
        existing_file = await files_repo.get_by_sha256(session, sha256)
        if existing_file is not None:
            await storage.delete(storage_key)
            return await _create_dedup_entry(
                session,
                file=existing_file,
                folder_id=folder_id,
                desired_name=desired_name,
                now=now,
            )

    return await _create_new_file_entry(
        session,
        file_id=tentative_file_id,
        storage_key=storage_key,
        sha256=sha256,
        size=size,
        content_type=content_type,
        fallback_name=fallback_name,
        folder_id=folder_id,
        desired_name=desired_name,
        now=now,
    )

async def _create_new_file_entry(
    session: AsyncSession,
    *,
    file_id: str,
    storage_key: str,
    sha256: str,
    size: int,
    content_type: str | None,
    fallback_name: str,
    folder_id: str | None,
    desired_name: str,
    now: datetime,
) -> UploadResult:
    file_row = File(
        id=file_id,
        storage_key=storage_key,
        sha256=sha256,
        size_bytes=size,
        mime_type=content_type,
        original_ext=_split_extension(fallback_name)[1] or None,
        kind=None,
        summary=None,
        description=None,
        extra=None,
        ingest_status="pending",
        ingested_at=None,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(file_row)
    await session.flush()
    await audit_events_repo.append(
        session,
        kind="file_created",
        payload={
            "file_id": file_row.id,
            "sha256": sha256,
            "size_bytes": size,
            "mime_type": content_type,
        },
    )

    final_name, auto_renamed = await _resolve_display_name(session, folder_id, desired_name)
    entry = FileEntry(
        id=new_id(),
        folder_id=folder_id or "",
        file_id=file_row.id,
        display_name=final_name,
        lifecycle="active",
        catalog_id=None,
        extra=None,
        deleted_at=None,
        purge_after=None,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    await session.flush()
    await audit_events_repo.append(
        session,
        kind="entry_created",
        payload={
            "entry_id": entry.id,
            "folder_id": folder_id,
            "file_id": file_row.id,
            "display_name": final_name,
            "deduped": False,
        },
    )

    task = await enqueue(
        session,
        kind=KIND_INGEST_FILE,
        payload={"file_id": file_row.id},
        dedup_key=f"ingest_file:{file_row.id}",
    )
    if task is not None:
        await audit_events_repo.append(
            session,
            kind="task_enqueued",
            payload={"task_id": task.id, "kind": KIND_INGEST_FILE, "file_id": file_row.id},
            task_id=task.id,
        )

    return UploadResult(
        file_id=file_row.id,
        entry_id=entry.id,
        folder_id=folder_id,
        display_name=final_name,
        deduped=False,
        auto_renamed=auto_renamed,
    )

async def _create_dedup_entry(
    session: AsyncSession,
    *,
    file: File,
    folder_id: str | None,
    desired_name: str,
    now: datetime,
) -> UploadResult:
    """sha256 already exists. Find a seed entry, copy AI fields, INSERT new entry."""
    seed = await entries_repo.find_seed_by_file_id(session, file.id)

    final_name, auto_renamed = await _resolve_display_name(session, folder_id, desired_name)
    entry = FileEntry(
        id=new_id(),
        folder_id=folder_id or "",
        file_id=file.id,
        display_name=final_name,
        lifecycle="active",
        catalog_id=seed.catalog_id if seed is not None else None,
        extra=seed.extra if seed is not None else None,
        deleted_at=None,
        purge_after=None,
        created_at=now,
        updated_at=now,
    )
    session.add(entry)
    await session.flush()

    if seed is not None:
        seed_tags = await entry_tags_repo.list_tag_ids_for_entry(session, seed.id)
        for tag_id in seed_tags:
            session.add(
                EntryTag(
                    entry_id=entry.id,
                    tag_id=tag_id,
                    source="dedup_seed",
                    created_at=now,
                )
            )

    await audit_events_repo.append(
        session,
        kind="entry_created",
        payload={
            "entry_id": entry.id,
            "folder_id": folder_id,
            "file_id": file.id,
            "display_name": final_name,
            "deduped": True,
            "seed_entry_id": seed.id if seed is not None else None,
        },
    )

    return UploadResult(
        file_id=file.id,
        entry_id=entry.id,
        folder_id=folder_id,
        display_name=final_name,
        deduped=True,
        auto_renamed=auto_renamed,
    )
