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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryTag, File, FileEntry
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import files as files_repo
from marginalia.services.folders import (
    FolderNotFoundError,
    resolve_or_create_folder,
    split_remote_path,
)
from marginalia.repositories import folders as folders_repo
from marginalia.storage.base import StorageBackend
from marginalia.storage.mirror import MirrorStorage
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.utils.hashing import StreamHasher
from marginalia.utils.ids import new_id, storage_prefix

_NameConflictPolicy = Literal["rename", "error", "skip"]

DEFAULT_ON_CONFLICT: _NameConflictPolicy = "rename"

def resolve_on_conflict(
    on_conflict: _NameConflictPolicy | None,
) -> _NameConflictPolicy:
    """Resolve the runtime default lazily so GUI settings take effect."""
    if on_conflict is not None:
        return on_conflict
    from marginalia.config import get_settings
    return get_settings().default_on_conflict

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

# Bound on how many ' (N)' bumps we try when the disk-claimed storage_key
# collides with a stale files.storage_key row (UNIQUE) in the DB.
_STORAGE_KEY_DB_RETRIES = 50

def _bump_suffix(name: str) -> str:
    """Next ' (N)' variant of a filename, matching the numbering used by
    _resolve_display_name / mirror._candidate_names. 'a.txt' -> 'a (1).txt';
    'a (1).txt' -> 'a (2).txt'."""
    stem, ext = _split_extension(name)
    if stem.endswith(")") and " (" in stem:
        head, _, num = stem.rpartition(" (")
        num = num[:-1]  # drop trailing ')'
        if head and num.isdigit():
            return f"{head} ({int(num) + 1}){ext}"
    return f"{stem} (1){ext}"

def _sibling_rel(storage_key: str, name: str) -> str:
    """Replace the basename of a storage_key with `name`, keeping its
    parent directory."""
    parent, _, _ = storage_key.rpartition("/")
    return f"{parent}/{name}" if parent else name

async def _storage_key_in_db(session: AsyncSession, storage_key: str) -> bool:
    return (
        await session.execute(
            select(File.id).where(File.storage_key == storage_key).limit(1)
        )
    ).first() is not None

async def _resolve_storage_key_db_collision(
    session: AsyncSession, storage: StorageBackend, storage_key: str,
) -> tuple[str, str]:
    """Mirror mode: the on-disk O_EXCL claim in MirrorStorage.put only
    guarantees a DISK-free path, but files.storage_key is UNIQUE. A live
    File row whose disk file the user deleted in Finder (not yet /sync'd)
    can still own this key, so the insert would raise IntegrityError (500)
    and orphan the just-written vault file. Move the object to the next
    ' (N)' candidate until the key is also free in the DB (bounded).

    Returns (storage_key, disk_basename) — the basename is adopted as the
    display_name so DB and vault keep the same string. The first
    iteration also covers the common no-collision case (and any
    sanitization put() applied)."""
    for _ in range(_STORAGE_KEY_DB_RETRIES):
        if not await _storage_key_in_db(session, storage_key):
            return storage_key, storage_key.rsplit("/", 1)[-1]
        next_name = _bump_suffix(storage_key.rsplit("/", 1)[-1])
        storage_key = await storage.rename(
            storage_key, _sibling_rel(storage_key, next_name),
        )
    await storage.delete(storage_key)
    raise RuntimeError(
        "could not find a storage_key free in both the vault and the "
        f"database after {_STORAGE_KEY_DB_RETRIES} attempts"
    )

async def upload(
    session: AsyncSession,
    storage: StorageBackend,
    *,
    stream: AsyncIterator[bytes],
    fallback_name: str,
    remote_path: str | None = None,
    folder_id: str | None = None,
    display_name: str | None = None,
    content_type: str | None = None,
    on_conflict: _NameConflictPolicy | None = None,
) -> UploadResult:
    """Upload a single file. Two destination styles, exactly one required:

      - `remote_path`: CLI/API style. See split_remote_path for the four
        legal forms. Folders along the path are auto-created.
      - `folder_id`: GUI style. Target folder already exists; display_name
        defaults to fallback_name.

    `display_name` (when given) overrides the name derived from either.
    """
    if (remote_path is None) == (folder_id is None):
        raise ValueError("exactly one of remote_path or folder_id is required")

    folder_display_path: str | None
    derived_name: str | None
    if folder_id is not None:
        folder = await folders_repo.get_live(session, folder_id)
        if folder is None:
            raise FolderNotFoundError(folder_id)
        derived_name = display_name
        resolved_folder_id: str | None = folder.id
        # Walk the folder chain so mirror mode lands the file in the
        # matching vault directory (deferred import: services.entries
        # imports from this module).
        from marginalia.services.entries import _build_folder_display_path
        folder_display_path = (
            await _build_folder_display_path(session, folder.id) or None
        )
    else:
        folder_segments, derived_name = split_remote_path(
            remote_path or "", display_name_override=display_name,
        )
        folder = await resolve_or_create_folder(session, folder_segments)
        resolved_folder_id = folder.id if folder is not None else None
        folder_display_path = (
            "/" + "/".join(folder_segments) if folder_segments else None
        )
    desired_name = (derived_name or fallback_name).strip()
    if not desired_name:
        raise ValueError("display_name and fallback_name both empty")
    on_conflict = resolve_on_conflict(on_conflict)

    folder_id_for_lookup = resolved_folder_id

    # --- early conflict check (skip / error short-circuit before reading bytes)
    if on_conflict in ("error", "skip"):
        clash = await _existing_entry_with_name(session, folder_id_for_lookup, desired_name)
        if clash is not None:
            if on_conflict == "error":
                raise DisplayNameConflictError(
                    folder_id=folder_id_for_lookup,
                    display_name=desired_name,
                    existing_entry_id=clash.id,
                    existing_file_id=clash.file_id,
                )
            return UploadResult(
                file_id=clash.file_id,
                entry_id=clash.id,
                folder_id=folder_id_for_lookup,
                display_name=desired_name,
                deduped=False,
                auto_renamed=False,
                skipped=True,
            )

    # --- resolve the final display_name BEFORE writing bytes so the DB
    # name and the mirror disk name are the same string (one numbering
    # convention, shared with mirror._candidate_names)
    final_name, auto_renamed = await _resolve_display_name(
        session, folder_id_for_lookup, desired_name,
    )

    # --- stream → storage at a tentative key (we don't yet know if dedup hits)
    tentative_file_id = new_id()
    tentative_storage_key = _make_storage_key(tentative_file_id)
    hasher = StreamHasher(stream)
    storage_key = await storage.put(
        tentative_storage_key, hasher.__aiter__(),
        content_type=content_type,
        display_name=final_name,
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

    if is_mirror:
        # Adopt the disk basename (put() may have sanitized/suffixed it)
        # AND resolve any UNIQUE(files.storage_key) collision with a stale
        # DB row before inserting — otherwise the flush 500s and orphans
        # the vault file we just wrote.
        storage_key, disk_name = await _resolve_storage_key_db_collision(
            session, storage, storage_key,
        )
        if disk_name != final_name:
            final_name = disk_name
            auto_renamed = final_name != desired_name

    if not is_mirror:
        existing_file = await files_repo.get_by_sha256(session, sha256)
        if existing_file is not None:
            await storage.delete(storage_key)
            return await _create_dedup_entry(
                session,
                file=existing_file,
                folder_id=folder_id_for_lookup,
                final_name=final_name,
                auto_renamed=auto_renamed,
                now=now,
            )

    try:
        return await _create_new_file_entry(
            session,
            file_id=tentative_file_id,
            storage_key=storage_key,
            sha256=sha256,
            size=size,
            content_type=content_type,
            fallback_name=fallback_name,
            folder_id=folder_id_for_lookup,
            final_name=final_name,
            auto_renamed=auto_renamed,
            now=now,
        )
    except Exception:
        # The row insert failed (e.g. a storage_key/UNIQUE race not caught
        # by the pre-check above). The stored object would otherwise be
        # orphaned — no row will ever reference it — so drop it before
        # re-raising. storage.delete swallows a missing file.
        await storage.delete(storage_key)
        raise

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
    final_name: str,
    auto_renamed: bool,
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

    entry = FileEntry(
        id=new_id(),
        folder_id=folder_id,
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
        payload={"file_id": file_row.id, "display_name": final_name},
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
    final_name: str,
    auto_renamed: bool,
    now: datetime,
) -> UploadResult:
    """sha256 already exists. Find a seed entry, copy AI fields, INSERT new entry."""
    seed = await entries_repo.find_seed_by_file_id(session, file.id)

    entry = FileEntry(
        id=new_id(),
        folder_id=folder_id,
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
