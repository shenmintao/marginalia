"""User-facing file operations — DESIGN.md §14.3 user view boundary.

Three user-side capabilities:
  - search_entries(query):     find entries by free-text in user fields +
                                content summary as a recall signal. The
                                response NEVER carries the summary back —
                                only display_name / folder / lifecycle / etc.
  - get_user_metadata(eid):    return user-visible metadata + the librarian's
                                short summary (the "label card" exception in
                                §14.3 #4), resolved tags, and entry-level
                                extra.  AI fields like description / catalog /
                                kind remain NOT exposed.
  - open_for_download(eid):    resolve to a (file_row, async iterator of
                                bytes) so the route can stream.

All three operations refuse soft-deleted entries.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry, Folder
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import folders as folders_repo
from marginalia.storage import get_storage
from marginalia.storage.base import StorageBackend


SEARCH_LIMIT_DEFAULT = 25
SEARCH_LIMIT_MAX = 100
SEARCH_RELATED_TOP_K = 3      # neighbours surfaced per search hit
METADATA_RELATED_TOP_K = 8    # neighbours surfaced on the single-entry page


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

    rows = await entries_repo.search_with_file(session, like=like, limit=limit)

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
            "related_entries": await _related_entries_for(
                session, entry.id, top_k=SEARCH_RELATED_TOP_K,
            ),
        })
    return out


async def _related_entries_for(
    session: AsyncSession, entry_id: str, *, top_k: int,
) -> list[dict[str, Any]]:
    """Pre-fill list — vetted-only neighbours of `entry_id` from the
    discovery layer, top-K by random walk score. Empty list if no
    vetted relations exist (silent — agent treats it as "no neighbours
    yet" rather than an error).

    Surfacing this in search/get_metadata is the point of the discovery
    layer: agents and CLI users see neighbours without having to ask
    for them, which is what cuts the loop count we'd otherwise spend on
    "search → see one match → search again for siblings"."""
    from marginalia.services.recommend import find_related as _walk
    rows = await _walk(session, seed_entry_id=entry_id, top_k=top_k)
    return [
        {
            "entry_id": r.entry_id,
            "display_name": r.display_name,
            "score": round(r.score, 4),
        }
        for r in rows
    ]


# ---- metadata -------------------------------------------------------------

async def get_user_metadata(
    session: AsyncSession,
    *,
    entry_id: str,
) -> dict[str, Any]:
    pair = await entries_repo.get_live_with_file(session, entry_id)
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
        # the user even though it is technically AI-written. DESIGN.md
        # §14.3 #4 carves this out as the legitimate cross-boundary view.
        "summary": file_row.summary,
        "preview": _description_preview(file_row.description),
        # Tags: resolved tag names with facets for the Library panel.
        "tags": await _tags_for_entry(session, entry.id),
        # Extra: per-entry mutable AI field. Falls back to per-file
        # immutable field when entry-level is empty.
        "extra": entry.extra or file_row.extra or None,
        "related_entries": await _related_entries_for(
            session, entry.id, top_k=METADATA_RELATED_TOP_K,
        ),
    }


def _description_preview(
    description: Any | None, *, max_sections: int = 3, max_chars: int = 320,
) -> list[dict[str, str]]:
    """Render the first few section summaries from `file_row.description`
    so `/info` can show what the file is *about* without a separate
    download. The librarian's section summaries are AI-written but the
    same boundary carve-out as `summary` applies (DESIGN.md §14.3 #4).

    Returns up to `max_sections` `{title, summary}` pairs. Truncates each
    summary at `max_chars` so a verbose section can't blow up the panel.
    Returns an empty list when description is missing or malformed.
    """
    if not isinstance(description, dict):
        return []
    sections = description.get("sections")
    if not isinstance(sections, list):
        return []
    out: list[dict[str, str]] = []
    for sec in sections[:max_sections]:
        if not isinstance(sec, dict):
            continue
        title = str(sec.get("title") or "").strip()
        summary = str(sec.get("summary") or "").strip()
        if len(summary) > max_chars:
            summary = summary[: max_chars - 1].rstrip() + "…"
        if title or summary:
            out.append({"title": title, "summary": summary})
    return out


# ---- download -------------------------------------------------------------

async def open_for_download(
    session: AsyncSession,
    *,
    entry_id: str,
    storage: StorageBackend | None = None,
) -> DownloadHandle:
    pair = await entries_repo.get_live_with_file(session, entry_id)
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
        children = await folders_repo.list_live_children_of_many(
            session, frontier,
        )
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
    rows = await entries_repo.list_live_with_file_in_folders(session, folder_ids)

    result: list[tuple[str, FileEntry, File]] = []
    for entry, file_row in rows:
        rel = rel_paths.get(entry.folder_id, "")
        zip_path = (rel + "/" + entry.display_name) if rel else entry.display_name
        result.append((zip_path, entry, file_row))
    return result


# ---- helpers --------------------------------------------------------------

async def _tags_for_entry(
    session: AsyncSession, entry_id: str,
) -> list[dict[str, str | None]]:
    """Resolved tags for display: `{name, facet}` for each canonical tag."""
    rows = await entry_tags_repo.list_name_facet_for_entry(session, entry_id)
    return [{"name": n, "facet": f} for n, f in rows]


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


async def get_entry_path(
    session: AsyncSession, *, entry_id: str,
) -> dict[str, Any]:
    """Resolve `entry_id` to its folder ancestor chain (root → leaf).

    Drives the GUI's "click a search hit → expand the Library tree to
    that file" navigation: the desktop side feeds the chain to the
    FolderTree as a controlled-expansion path so each ancestor opens
    in order. Root-folder entries return an empty chain.
    """
    pair = await entries_repo.get_live_with_file(session, entry_id)
    if pair is None:
        raise EntryNotFoundError(entry_id)
    entry, _ = pair

    chain: list[dict[str, str]] = []
    cur: str | None = entry.folder_id
    while cur is not None:
        f = await session.get(Folder, cur)
        if f is None or f.deleted_at is not None:
            break
        chain.append({"id": f.id, "name": f.name})
        cur = f.parent_id
    chain.reverse()
    return {
        "entry_id": entry.id,
        "display_name": entry.display_name,
        "folder_id": entry.folder_id,
        "ancestors": chain,
    }
