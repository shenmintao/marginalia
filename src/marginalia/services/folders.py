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

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Folder
from marginalia.services.audit import write_event
from marginalia.utils.ids import new_id


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
      1. trailing "/"               → ALL segments are folders. display_name = None
                                       (caller falls back to local basename).
      2. last segment contains "."  → folders = parts[:-1], display_name = last.
      3. otherwise (no "." AND no trailing "/"):
         - if `display_name_override` given → folders = ALL parts, display_name = override
         - else                              → AmbiguousRemotePathError

    The third rule resolves git-style ambiguity (LICENSE, Dockerfile, .env in
    the middle of a path): the client must EITHER mark the path as a folder
    with a trailing slash, OR pass an explicit display_name parameter.

    Examples:
        ("/a/b/")                          → (["a","b"], None)
        ("/a/b")                           → AmbiguousRemotePathError
        ("/a/b", display_name_override=X)  → (["a","b"], X)
        ("/a/b/foo.pdf")                   → (["a","b"], "foo.pdf")
        ("")                               → ([], None)
    """
    s = (remote or "").strip()
    if not s or s == "/":
        return [], display_name_override

    trailing_slash = s.endswith("/")
    parts = [p for p in s.strip("/").split("/") if p]
    if not parts:
        return [], display_name_override

    if trailing_slash:
        # path is purely a folder. display_name override still wins.
        return parts, display_name_override

    last = parts[-1]
    if "." in last:
        # last segment looks like a filename. Override wins if both given.
        return parts[:-1], (display_name_override or last)

    # ambiguous: no dot, no trailing slash
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
    session: AsyncSession, segments: list[str]
) -> Folder | None:
    """Walk / create the folder chain. Returns the deepest folder (or None for root).

    Each segment is matched against `(parent_id, name)`. Missing segments are
    inserted in order. Each insert emits a `folder_created` audit event.
    """
    if not segments:
        return None

    parent: Folder | None = None
    for name in segments:
        parent = await _find_or_create_child(session, parent, name)
    return parent


async def _find_or_create_child(
    session: AsyncSession, parent: Folder | None, name: str
) -> Folder:
    parent_id = parent.id if parent is not None else None
    stmt = select(Folder).where(
        Folder.parent_id.is_(parent_id) if parent_id is None else Folder.parent_id == parent_id,
        Folder.name == name,
        Folder.deleted_at.is_(None),
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    folder = Folder(
        id=new_id(),
        parent_id=parent_id,
        name=name,
        created_at=now,
        updated_at=now,
    )
    session.add(folder)
    await session.flush()
    await write_event(
        session,
        kind="folder_created",
        payload={
            "folder_id": folder.id,
            "parent_id": parent_id,
            "name": name,
            "auto_created": True,
        },
    )
    return folder


async def list_root_folders(session: AsyncSession) -> list[Folder]:
    return await _list_children(session, None)


async def list_child_folders(session: AsyncSession, parent_id: str) -> list[Folder]:
    return await _list_children(session, parent_id)


async def _list_children(
    session: AsyncSession, parent_id: str | None
) -> list[Folder]:
    stmt = (
        select(Folder)
        .where(
            Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
            Folder.deleted_at.is_(None),
        )
        .order_by(Folder.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_folder(session: AsyncSession, folder_id: str) -> Folder | None:
    return (
        await session.execute(
            select(Folder).where(Folder.id == folder_id, Folder.deleted_at.is_(None))
        )
    ).scalar_one_or_none()


# ---- user-side mutations (design.md §14.1) ---------------------------------

class FolderNotFoundError(Exception):
    pass


class FolderNameConflictError(Exception):
    """Raised when renaming/moving a folder would collide with a sibling."""

    def __init__(self, *, parent_id: str | None, name: str, existing_id: str) -> None:
        super().__init__(f"folder name {name!r} already exists under parent {parent_id!r}")
        self.parent_id = parent_id
        self.name = name
        self.existing_id = existing_id


async def _name_taken(
    session: AsyncSession, *, parent_id: str | None, name: str, exclude_id: str | None
) -> str | None:
    stmt = select(Folder.id).where(
        Folder.parent_id.is_(None) if parent_id is None else Folder.parent_id == parent_id,
        Folder.name == name,
        Folder.deleted_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(Folder.id != exclude_id)
    return (await session.execute(stmt.limit(1))).scalar_one_or_none()


async def _would_cycle(
    session: AsyncSession, *, child_id: str, new_parent_id: str | None
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
        f = await session.get(Folder, cur)
        if f is None:
            return False
        cur = f.parent_id
    return False


async def rename_folder(
    session: AsyncSession, *, folder_id: str, new_name: str
) -> Folder:
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("folder name cannot be empty")
    f = await get_folder(session, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)
    if f.name == new_name:
        return f
    clash = await _name_taken(
        session, parent_id=f.parent_id, name=new_name, exclude_id=f.id
    )
    if clash is not None:
        raise FolderNameConflictError(
            parent_id=f.parent_id, name=new_name, existing_id=clash
        )
    old = f.name
    f.name = new_name
    f.updated_at = datetime.now(timezone.utc)
    from marginalia.services.audit import write_event
    await write_event(session, kind="folder_renamed", payload={
        "folder_id": f.id, "parent_id": f.parent_id,
        "old_name": old, "new_name": new_name,
    })
    return f


async def move_folder(
    session: AsyncSession, *, folder_id: str, new_parent_id: str | None
) -> Folder:
    f = await get_folder(session, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)
    if f.parent_id == new_parent_id:
        return f
    if new_parent_id is not None:
        target = await get_folder(session, new_parent_id)
        if target is None:
            raise FolderNotFoundError(new_parent_id)
    if await _would_cycle(session, child_id=f.id, new_parent_id=new_parent_id):
        raise ValueError(f"move would create folder cycle: {f.id} → {new_parent_id}")
    clash = await _name_taken(
        session, parent_id=new_parent_id, name=f.name, exclude_id=f.id
    )
    if clash is not None:
        raise FolderNameConflictError(
            parent_id=new_parent_id, name=f.name, existing_id=clash
        )
    old_parent = f.parent_id
    f.parent_id = new_parent_id
    f.updated_at = datetime.now(timezone.utc)
    from marginalia.services.audit import write_event
    await write_event(session, kind="folder_moved", payload={
        "folder_id": f.id, "old_parent": old_parent, "new_parent": new_parent_id,
    })
    return f


async def soft_delete_folder(
    session: AsyncSession,
    *,
    folder_id: str,
    purge_after_seconds: int = 7 * 86400,
) -> Folder:
    """Soft-delete a folder: set deleted_at on the folder and recursively
    soft-delete all live descendants + entries inside (with the same
    purge_after window). The actual storage / row deletion is done by
    purge_deleted_files later."""
    from datetime import timedelta

    from marginalia.db.models import FileEntry
    from marginalia.services.audit import write_event

    f = await get_folder(session, folder_id)
    if f is None:
        raise FolderNotFoundError(folder_id)

    now = datetime.now(timezone.utc)
    purge_at = now + timedelta(seconds=max(0, purge_after_seconds))

    # collect descendant folder ids (BFS over live children)
    descendant_folder_ids: list[str] = [f.id]
    frontier = [f.id]
    while frontier:
        children = (
            await session.execute(
                select(Folder.id).where(
                    Folder.parent_id.in_(frontier),
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not children:
            break
        descendant_folder_ids.extend(children)
        frontier = list(children)

    # mark folders as deleted
    n_folders = 0
    for fid in descendant_folder_ids:
        fld = await session.get(Folder, fid)
        if fld is None or fld.deleted_at is not None:
            continue
        fld.deleted_at = now
        fld.updated_at = now
        n_folders += 1

    # mark all live entries inside these folders as deleted
    entries = (
        await session.execute(
            select(FileEntry).where(
                FileEntry.folder_id.in_(descendant_folder_ids),
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    for e in entries:
        e.deleted_at = now
        e.purge_after = purge_at
        e.updated_at = now

    await write_event(session, kind="folder_soft_deleted", payload={
        "folder_id": f.id,
        "name": f.name,
        "descendant_folders_marked": n_folders,
        "entries_marked": len(entries),
        "purge_after": purge_at.isoformat(),
    })
    return f
