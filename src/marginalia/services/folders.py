"""Folder service.

Encodes the CLI semantics: `upload <local> <remote>` where the remote path is
a single absolute string (e.g. `/research/llm/foo.pdf` or `/research/llm/`).
The route handler asks this service to:

  1. Split `<remote>` into (folder_segments, display_name) — see split_remote_path
  2. Walk / auto-create the folder chain (resolve_or_create_folder)

Identity: folders are user-owned. This module never reads or writes AI-internal
fields. Auto-creating a folder on upload is treated as the user implicitly
creating it (which they did, by naming it in the path).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Folder
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import folders as folders_repo
from marginalia.utils.ids import new_id

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

class AmbiguousRemotePathError(ValueError):
    """Raised when a remote path's intent (file vs folder) is unresolvable.

    Specifically: the last segment has no '.' AND no trailing '/', so it
    could be either a folder or a no-extension file (LICENSE, Makefile, …).
    The caller must either add a trailing '/' (folder) or pass an explicit
    `display_name` (file).
    """

    def __init__(self, remote: str) -> None:
        super().__init__(
            f"remote path {remote!r} is ambiguous: last segment has no extension "
            f"and no trailing '/'. Add '/' to mean folder, or supply display_name "
            f"to mean file."
        )
        self.remote = remote

def split_remote_path(
    remote: str,
    *,
    display_name_override: str | None = None,
) -> tuple[list[str], str | None]:
    """Split `<remote>` into (folder_segments, display_name).

    Rules (Cycle 12 final):
      1. trailing "/"               -> ALL segments are folders. display_name = None
                                       (caller falls back to local basename).
      2. last segment contains "."  -> folders = parts[:-1], display_name = last.
      3. otherwise (no "." AND no trailing "/"):
         - if `display_name_override` given -> folders = ALL parts, display_name = override
         - else                              -> AmbiguousRemotePathError

    The third rule resolves git-style ambiguity (LICENSE, Dockerfile, .env in
    the middle of a path): the client must EITHER mark the path as a folder
    with a trailing slash, OR pass an explicit display_name parameter.

    Examples:
        ("/a/b/")                          -> (["a","b"], None)
        ("/a/b")                           -> AmbiguousRemotePathError
        ("/a/b", display_name_override=X)  -> (["a","b"], X)
        ("/a/b/foo.pdf")                   -> (["a","b"], "foo.pdf")
        ("")                               -> ([], None)
    """
    s = (remote or "").strip()
    if not s or s == "/":
        return [], display_name_override

    trailing_slash = s.endswith("/")
    parts = [p for p in s.strip("/").split("/") if p]
    if not parts:
        return [], display_name_override

    if trailing_slash:
        return parts, display_name_override

    last = parts[-1]
    if "." in last:
        return parts[:-1], (display_name_override or last)

    if display_name_override is not None:
        return parts, display_name_override
    raise AmbiguousRemotePathError(remote)

def parse_remote_folder(remote: str) -> list[str]:
    """Pure-folder split, ignoring file-name heuristics. Used internally where
    the caller has already decided the remote is a folder."""
    s = (remote or "").strip()
    if not s or s == "/":
        return []
    return [p for p in s.strip("/").split("/") if p]

async def resolve_or_create_folder(
    db: AsyncSession, segments: list[str]
) -> Folder | None:
    """Walk / create the folder chain. Returns the deepest folder (or None for root).

    Each segment is matched against `(parent_id, name)`. Missing segments are
    inserted in order. Each insert emits a `folder_created` audit event.
    """
    if not segments:
        return None
    parent: Folder | None = None
    for name in segments:
        parent = await _find_or_create_child(db, parent, name)
    return parent

async def _find_or_create_child(
    db: AsyncSession, parent: Folder | None, name: str
) -> Folder:
    parent_id = parent.id if parent is not None else None
    existing = await folders_repo.find_child_by_name(
        db, parent_id=parent_id, name=name,
    )
    if existing is not None:
        return existing

    now = _utcnow()
    folder = Folder(
        id=new_id(),
        parent_id=parent_id,
        name=name,
        created_at=now,
        updated_at=now,
    )
    db.add(folder)
    await db.flush()
    await audit_events_repo.append(
        db, kind="folder_created", payload={
            "folder_id": folder.id, "parent_id": parent_id,
            "name": name, "auto_created": True,
        },
    )
    return folder

async def create_folder(
    db: AsyncSession, *, parent_id: str | None, name: str,
) -> Folder:
    """Create a single empty folder under `parent_id` (None = root).

    Mirrors `_find_or_create_child` but never returns an existing match: a
    name clash with a live sibling raises FolderNameConflictError. Audit
    event records `auto_created=False` to distinguish from upload-driven
    folder creation.
    """
    name = name.strip()
    if not name:
        raise ValueError("folder name cannot be empty")
    if "/" in name or "\\" in name:
        raise ValueError("folder name may not contain '/' or '\\'")
    if parent_id is not None:
        parent = await folders_repo.get_live(db, parent_id)
        if parent is None:
            raise FolderNotFoundError(parent_id)
    clash = await folders_repo.find_child_by_name(
        db, parent_id=parent_id, name=name,
    )
    if clash is not None:
        raise FolderNameConflictError(
            parent_id=parent_id, name=name, existing_id=clash.id,
        )
    now = _utcnow()
    folder = Folder(
        id=new_id(),
        parent_id=parent_id,
        name=name,
        created_at=now,
        updated_at=now,
    )
    db.add(folder)
    await db.flush()
    await audit_events_repo.append(
        db, kind="folder_created", payload={
            "folder_id": folder.id, "parent_id": parent_id,
            "name": name, "auto_created": False,
        },
    )
    return folder


async def list_root_folders(db: AsyncSession) -> list[Folder]:
    return await folders_repo.list_children(db, None)

async def list_child_folders(db: AsyncSession, parent_id: str) -> list[Folder]:
    return await folders_repo.list_children(db, parent_id)

async def get_folder(db: AsyncSession, folder_id: str) -> Folder | None:
    return await folders_repo.get_live(db, folder_id)

# ---- user-side mutations (DESIGN.md §14.1) ---------------------------------

class FolderNotFoundError(Exception):
    pass

class FolderNameConflictError(Exception):
    """Raised when renaming/moving a folder would collide with a sibling."""

    def __init__(self, *, parent_id: str | None, name: str, existing_id: str) -> None:
        super().__init__(f"folder name {name!r} already exists under parent {parent_id!r}")
        self.parent_id = parent_id
        self.name = name
        self.existing_id = existing_id

async def _would_cycle(
    db: AsyncSession, *, child_id: str, new_parent_id: str | None
) -> bool:
    if new_parent_id is None:
        return False
    if new_parent_id == child_id:
        return True
    cur: str | None = new_parent_id
    seen: set[str] = {child_id}
    while cur is not None:
        if cur in seen:
            return True
        seen.add(cur)
        f = await db.get(Folder, cur)
        if f is None:
            return False
        cur = f.parent_id
    return False

async def rename_folder(
    db: AsyncSession, *, folder_id: str, new_name: str,
) -> Folder:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("folder name cannot be empty")
    f = await folders_repo.get_live(db, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)
    if f.name == new_name:
        return f
    clash = await folders_repo.find_sibling_id_by_name(
        db, parent_id=f.parent_id, name=new_name, exclude_id=f.id,
    )
    if clash is not None:
        raise FolderNameConflictError(
            parent_id=f.parent_id, name=new_name, existing_id=clash,
        )
    old = f.name
    f.name = new_name
    f.updated_at = _utcnow()
    await audit_events_repo.append(db, kind="folder_renamed", payload={
        "folder_id": f.id, "parent_id": f.parent_id,
        "old_name": old, "new_name": new_name,
    })
    return f

async def move_folder(
    db: AsyncSession, *, folder_id: str, new_parent_id: str | None,
) -> Folder:
    f = await folders_repo.get_live(db, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)
    if f.parent_id == new_parent_id:
        return f
    if new_parent_id is not None:
        target = await folders_repo.get_live(db, new_parent_id)
        if target is None:
            raise FolderNotFoundError(new_parent_id)
    if await _would_cycle(db, child_id=f.id, new_parent_id=new_parent_id):
        raise ValueError(f"move would create folder cycle: {f.id} -> {new_parent_id}")
    clash = await folders_repo.find_sibling_id_by_name(
        db, parent_id=new_parent_id, name=f.name, exclude_id=f.id,
    )
    if clash is not None:
        raise FolderNameConflictError(
            parent_id=new_parent_id, name=f.name, existing_id=clash,
        )
    old_parent = f.parent_id
    f.parent_id = new_parent_id
    f.updated_at = _utcnow()
    await audit_events_repo.append(db, kind="folder_moved", payload={
        "folder_id": f.id, "old_parent": old_parent, "new_parent": new_parent_id,
    })
    return f

async def soft_delete_folder(
    db: AsyncSession,
    *,
    folder_id: str,
    purge_after_seconds: int = 7 * 86400,
) -> Folder:
    """Soft-delete a folder and recursively soft-delete every live descendant
    folder + entries inside (with the same purge_after window). Storage / row
    deletion happens later in purge_deleted_files."""
    f = await folders_repo.get_live(db, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)

    now = _utcnow()
    purge_at = now + timedelta(seconds=max(0, purge_after_seconds))

    descendant_ids = await folders_repo.list_live_descendant_ids(db, f.id)

    n_folders = 0
    for fid in descendant_ids:
        fld = await db.get(Folder, fid)
        if fld is None or fld.deleted_at is not None:
            continue
        fld.deleted_at = now
        fld.updated_at = now
        n_folders += 1

    entries = await folders_repo.list_live_entries_in(db, descendant_ids)
    for e in entries:
        e.deleted_at = now
        e.purge_after = purge_at
        e.updated_at = now

    await audit_events_repo.append(db, kind="folder_soft_deleted", payload={
        "folder_id": f.id,
        "name": f.name,
        "descendant_folders_marked": n_folders,
        "entries_marked": len(entries),
        "purge_after": purge_at.isoformat(),
    })
    return f
