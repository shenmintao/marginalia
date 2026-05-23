"""User-facing file operations — design.md §14.3 user view boundary.

Three user-side capabilities:
  - search_entries(query):     find entries by free-text in user fields +
                                content summary as a recall signal. The
                                response NEVER carries the summary back —
                                only display_name / folder / lifecycle / etc.
  - get_user_metadata(eid):    return user-visible metadata + the librarian's
                                short summary (the "label card" exception in
                                §14.3 #4).  AI fields like description /
                                catalog / tags / extra are NOT exposed.
  - open_for_download(eid):    resolve to a (file_row, async iterator of
                                bytes) so the route can stream.

All three operations refuse soft-deleted entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry, Folder
from marginalia.storage import get_storage
from marginalia.storage.base import StorageBackend


SEARCH_LIMIT_DEFAULT = 25
SEARCH_LIMIT_MAX = 100


class EntryNotFoundError(Exception):
    pass


@dataclass(slots=True)
class DownloadHandle:
    file_id: str
    storage_key: str
    display_name: str
    mime_type: str
    size_bytes: int
    stream: AsyncIterator[bytes]


# ---- search ----------------------------------------------------------------

async def search_entries(
    session: AsyncSession,
    *,
    query: str,
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> list[dict[str, Any]]:
    """Return user-visible matches for `query`.

    Recall fields (used to find candidates): display_name, folder.name,
    files.summary. Response fields (returned to the user): display_name,
    folder_id, folder_path, lifecycle, mime_type, size_bytes, created_at,
    updated_at, ingest_status. files.summary is intentionally NOT returned —
    only used for recall.
    """
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(limit, SEARCH_LIMIT_MAX))
    like = f"%{q}%"

    rows = (
        await session.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
                or_(
                    FileEntry.display_name.ilike(like),
                    File.summary.ilike(like),
                    File.original_ext.ilike(like),
                ),
            )
            .order_by(FileEntry.updated_at.desc())
            .limit(limit)
        )
    ).all()

    out: list[dict[str, Any]] = []
    for entry, file_row in rows:
        folder_path = await _build_folder_path(session, entry.folder_id)
        out.append({
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "folder_id": entry.folder_id,
            "folder_path": folder_path,
            "lifecycle": entry.lifecycle,
            "mime_type": file_row.mime_type,
            "size_bytes": file_row.size_bytes,
            "ingest_status": file_row.ingest_status,
            "created_at": (
                entry.created_at.isoformat() if entry.created_at else None
            ),
            "updated_at": (
                entry.updated_at.isoformat() if entry.updated_at else None
            ),
        })
    return out


# ---- metadata -------------------------------------------------------------

async def get_user_metadata(
    session: AsyncSession,
    *,
    entry_id: str,
) -> dict[str, Any]:
    pair = (
        await session.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id == entry_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        raise EntryNotFoundError(entry_id)
    entry, file_row = pair

    folder_path = await _build_folder_path(session, entry.folder_id)

    return {
        "entry_id": entry.id,
        "file_id": file_row.id,
        "display_name": entry.display_name,
        "folder_id": entry.folder_id,
        "folder_path": folder_path,
        "lifecycle": entry.lifecycle,
        "mime_type": file_row.mime_type,
        "original_ext": file_row.original_ext,
        "size_bytes": file_row.size_bytes,
        "sha256": file_row.sha256,
        "ingest_status": file_row.ingest_status,
        "created_at": (
            entry.created_at.isoformat() if entry.created_at else None
        ),
        "updated_at": (
            entry.updated_at.isoformat() if entry.updated_at else None
        ),
        # The "label card" — the librarian's one-line summary is shown to
        # the user even though it is technically AI-written. design.md
        # §14.3 #4 carves this out as the legitimate cross-boundary view.
        "summary": file_row.summary,
    }


# ---- download -------------------------------------------------------------

async def open_for_download(
    session: AsyncSession,
    *,
    entry_id: str,
    storage: StorageBackend | None = None,
) -> DownloadHandle:
    pair = (
        await session.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id == entry_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        raise EntryNotFoundError(entry_id)
    entry, file_row = pair

    storage = storage or get_storage()
    return DownloadHandle(
        file_id=file_row.id,
        storage_key=file_row.storage_key,
        display_name=entry.display_name,
        mime_type=file_row.mime_type or "application/octet-stream",
        size_bytes=file_row.size_bytes or 0,
        stream=storage.get(file_row.storage_key),
    )


# ---- folder download (zip stream) -----------------------------------------

class FolderNotFoundError(Exception):
    pass


async def collect_folder_entries(
    session: AsyncSession,
    *,
    folder_id: str,
) -> list[tuple[str, FileEntry, File]]:
    """Walk the folder subtree, returning (relative_zip_path, entry, file)
    for every live entry inside. relative_zip_path is folder-relative so
    nested folders show up as nested zip directories.

    Raises FolderNotFoundError if the root folder is missing or soft-deleted.
    """
    root = await session.get(Folder, folder_id)
    if root is None or root.deleted_at is not None:
        raise FolderNotFoundError(folder_id)

    # BFS over folders, recording each folder's relative path
    rel_paths: dict[str, str] = {root.id: ""}
    frontier = [root.id]
    while frontier:
        children = (
            await session.execute(
                select(Folder).where(
                    Folder.parent_id.in_(frontier),
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        if not children:
            break
        next_frontier: list[str] = []
        for ch in children:
            parent_rel = rel_paths[ch.parent_id]
            rel_paths[ch.id] = (parent_rel + "/" if parent_rel else "") + ch.name
            next_frontier.append(ch.id)
        frontier = next_frontier

    folder_ids = list(rel_paths.keys())
    if not folder_ids:
        return []
    rows = (
        await session.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.folder_id.in_(folder_ids),
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
            .order_by(FileEntry.folder_id, FileEntry.display_name)
        )
    ).all()

    result: list[tuple[str, FileEntry, File]] = []
    for entry, file_row in rows:
        rel = rel_paths.get(entry.folder_id, "")
        zip_path = (rel + "/" + entry.display_name) if rel else entry.display_name
        result.append((zip_path, entry, file_row))
    return result


# ---- helpers --------------------------------------------------------------

async def _build_folder_path(
    session: AsyncSession, folder_id: str | None
) -> str:
    if not folder_id:
        return "/"
    parts: list[str] = []
    cur: str | None = folder_id
    while cur is not None:
        f = await session.get(Folder, cur)
        if f is None or f.deleted_at is not None:
            break
        parts.append(f.name)
        cur = f.parent_id
    return "/" + "/".join(reversed(parts))
