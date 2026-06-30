"""User-side search / metadata / download routes (file + folder zip)."""
from __future__ import annotations

import io
import re
import zipfile
from typing import Any, AsyncIterator, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.api.http_headers import content_disposition
from marginalia.db.models import Folder
from marginalia.db.session import get_session
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.services.recommend import find_related
from marginalia.services.relation_vetting import schedule_direct_relation_vetting
from marginalia.services.user_files import (
    DownloadHandle,
    EntryNotFoundError,
    EntryPreviewError,
    EntryRemoteNotHydratedError,
    EntryPreviewUnsupportedError,
    FolderNotFoundError,
    FolderRemoteNotHydratedError,
    collect_folder_entries,
    get_entry_path,
    get_user_metadata,
    open_extracted_text_preview,
    open_for_download,
    search_entries,
)
from marginalia.storage import get_storage

router = APIRouter(tags=["user_files"])

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entries = await search_entries(session, query=q, limit=limit)
    return {"q": q, "entries": entries, "count": len(entries)}


@router.get("/discover/{entry_id}")
async def discover(
    entry_id: str,
    top_k: int = Query(default=8, ge=1, le=30),
    include_unvetted: bool = Query(default=False),
    vet: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Random-walk recommendation from a seed entry. Drives the
    `/discover` REPL command and the related_entries pre-fill in
    search/get_metadata. Vetted edges only by default; pass
    include_unvetted=true to walk the raw graph. Pass vet=true to queue
    background vetting for the seed's direct raw edges; the response itself
    stays pure-read."""
    vetting = None
    if vet:
        scheduled = await schedule_direct_relation_vetting(
            session,
            entry_id=entry_id,
            limit=top_k,
        )
        vetting = {
            "requested": scheduled.requested,
            "candidates_available": scheduled.candidates_available,
            "queued": scheduled.queued,
            "task_id": scheduled.task_id,
        }
        if scheduled.task_id is not None:
            await audit_events_repo.append(
                session,
                kind="task_enqueued",
                task_id=scheduled.task_id,
                payload={
                    "kind": "vet_relations",
                    "entry_id": entry_id,
                    "scheduled_by": "discover:explicit",
                },
            )
        await session.commit()
    rows = await find_related(
        session, seed_entry_id=entry_id, top_k=top_k,
        include_unvetted=include_unvetted,
    )
    return {
        "seed_entry_id": entry_id,
        "results": [
            {
                "entry_id": r.entry_id,
                "display_name": r.display_name,
                "score": round(r.score, 4),
                "visit_count": r.visit_count,
                "direct_edge_weight": r.direct_edge_weight,
            }
            for r in rows
        ],
        "count": len(rows),
        "vetting": vetting,
    }


@router.get("/file-entries/{entry_id}/metadata")
async def file_entry_metadata(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await get_user_metadata(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")


@router.get("/file-entries/{entry_id}/path")
async def file_entry_path(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Folder ancestor chain (root → leaf) for an entry.

    The desktop GUI calls this when the user clicks a search hit or a
    chat citation, so the Library tree can expand each ancestor in
    order before selecting the file. Returns 404 if the entry is
    soft-deleted or unknown.
    """
    try:
        return await get_entry_path(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")


@router.get("/file-entries/{entry_id}/preview-text")
async def file_entry_preview_text(
    entry_id: str,
    max_chars: int = Query(default=2_000_000, ge=1, le=5_000_000),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        preview = await open_extracted_text_preview(
            session,
            entry_id=entry_id,
            max_chars=max_chars,
        )
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")
    except EntryPreviewUnsupportedError:
        raise HTTPException(status_code=415, detail="text preview not supported")
    except EntryRemoteNotHydratedError:
        raise HTTPException(status_code=409, detail="entry is remote; hydrate first")
    except EntryPreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {
        "entry_id": preview.entry_id,
        "file_id": preview.file_id,
        "display_name": preview.display_name,
        "pipeline": preview.pipeline,
        "text": preview.text,
        "total_chars": preview.total_chars,
        "returned_chars": preview.returned_chars,
        "truncated": preview.truncated,
    }


@router.get("/file-entries/{entry_id}/content")
async def file_entry_content(
    entry_id: str,
    range_header: str | None = Header(default=None, alias="Range"),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Inline-disposition variant of `/download` — used by the viewer
    iframe so PDFs and images render in the browser instead of getting
    saved to disk. The `/download` endpoint still exists for the
    explicit Download button (forces save-as)."""
    try:
        handle = await open_for_download(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")
    except EntryRemoteNotHydratedError:
        raise HTTPException(status_code=409, detail="entry is remote; hydrate first")

    headers = _file_content_headers(handle, disposition="inline")
    byte_range = _parse_range_header(range_header, handle.size_bytes)
    if byte_range is None:
        headers["Content-Length"] = str(handle.size_bytes)
        return StreamingResponse(
            handle.stream,
            media_type=handle.mime_type,
            headers=headers,
        )

    start, end = byte_range
    body = await get_storage().get_range(handle.storage_key, start, end)
    headers["Content-Range"] = f"bytes {start}-{end}/{handle.size_bytes}"
    headers["Content-Length"] = str(len(body))
    return StreamingResponse(
        _single_chunk(body),
        media_type=handle.mime_type,
        headers=headers,
        status_code=206,
    )


@router.get("/file-entries/{entry_id}/download")
async def file_entry_download(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    try:
        handle = await open_for_download(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")
    except EntryRemoteNotHydratedError:
        raise HTTPException(status_code=409, detail="entry is remote; hydrate first")

    headers = {
        "Content-Disposition": content_disposition("attachment", handle.display_name),
        "X-File-Id": handle.file_id,
        "X-Size-Bytes": str(handle.size_bytes),
    }
    return StreamingResponse(
        handle.stream,
        media_type=handle.mime_type,
        headers=headers,
    )


def _file_content_headers(
    handle: DownloadHandle,
    *,
    disposition: str,
) -> dict[str, str]:
    etag = f'"{handle.sha256}"' if handle.sha256 else (
        f'W/"{handle.file_id}-{handle.size_bytes}"'
    )
    return {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(disposition, handle.display_name),
        "ETag": etag,
        "X-File-Id": handle.file_id,
        "X-Size-Bytes": str(handle.size_bytes),
    }


def _parse_range_header(
    value: str | None,
    size: int,
) -> tuple[int, int] | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if "," in value:
        _raise_range_not_satisfiable(size)
    match = _RANGE_RE.match(value)
    if match is None:
        _raise_range_not_satisfiable(size)
    start_raw, end_raw = match.groups()
    if not start_raw and not end_raw:
        _raise_range_not_satisfiable(size)
    if size <= 0:
        _raise_range_not_satisfiable(size)

    if not start_raw:
        suffix_len = _parse_non_negative_int(end_raw, size)
        if suffix_len <= 0:
            _raise_range_not_satisfiable(size)
        start = max(size - suffix_len, 0)
        return start, size - 1

    start = _parse_non_negative_int(start_raw, size)
    end = size - 1 if not end_raw else _parse_non_negative_int(end_raw, size)
    if start >= size or end < start:
        _raise_range_not_satisfiable(size)
    return start, min(end, size - 1)


def _parse_non_negative_int(value: str, size: int) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _raise_range_not_satisfiable(size)
    if parsed < 0:
        _raise_range_not_satisfiable(size)
    return parsed


def _raise_range_not_satisfiable(size: int) -> NoReturn:
    raise HTTPException(
        status_code=416,
        detail="range not satisfiable",
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes */{max(size, 0)}",
        },
    )


async def _single_chunk(body: bytes) -> AsyncIterator[bytes]:
    yield body


# ---- folder download → zip stream -----------------------------------------

ZIP_CHUNK_SIZE = 64 * 1024


@router.get("/folders/{folder_id}/download")
async def folder_download(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    try:
        members = await collect_folder_entries(session, folder_id=folder_id)
    except FolderNotFoundError:
        raise HTTPException(status_code=404, detail="folder not found")
    except FolderRemoteNotHydratedError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "folder_contains_remote_entries",
                "message": "folder contains remote entries; hydrate them first",
                "entry_ids": exc.entry_ids,
            },
        )

    root_folder = await session.get(Folder, folder_id)
    archive_name = (root_folder.name if root_folder else "folder") + ".zip"

    # Materialise all storage keys eagerly while the session is alive — the
    # zip stream below runs after the dependency closes the session.
    plan: list[tuple[str, str]] = [(zp, file_row.storage_key)
                                   for zp, _entry, file_row in members]

    storage = get_storage()

    async def _zip_stream() -> AsyncIterator[bytes]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for zip_path, storage_key in plan:
                body = bytearray()
                async for chunk in storage.get(storage_key):
                    body.extend(chunk)
                zf.writestr(zip_path, bytes(body))
        buf.seek(0)
        while True:
            chunk = buf.read(ZIP_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    headers = {
        "Content-Disposition": content_disposition("attachment", archive_name),
        "X-Folder-Id": folder_id,
        "X-Member-Count": str(len(plan)),
    }
    return StreamingResponse(
        _zip_stream(),
        media_type="application/zip",
        headers=headers,
    )
