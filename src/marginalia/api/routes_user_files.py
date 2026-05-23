"""User-side search / metadata / download routes (file + folder zip)."""
from __future__ import annotations

import io
import zipfile
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Folder
from marginalia.db.session import get_session
from marginalia.services.user_files import (
    EntryNotFoundError,
    FolderNotFoundError,
    collect_folder_entries,
    get_user_metadata,
    open_for_download,
    search_entries,
)
from marginalia.storage import get_storage

router = APIRouter(tags=["user_files"])


@router.get("/search")
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    entries = await search_entries(session, query=q, limit=limit)
    return {"q": q, "entries": entries, "count": len(entries)}


@router.get("/file-entries/{entry_id}/metadata")
async def file_entry_metadata(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await get_user_metadata(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")


@router.get("/file-entries/{entry_id}/download")
async def file_entry_download(
    entry_id: str,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    try:
        handle = await open_for_download(session, entry_id=entry_id)
    except EntryNotFoundError:
        raise HTTPException(status_code=404, detail="entry not found")

    headers = {
        "Content-Disposition": f'attachment; filename="{handle.display_name}"',
        "X-File-Id": handle.file_id,
        "X-Size-Bytes": str(handle.size_bytes),
    }
    return StreamingResponse(
        handle.stream,
        media_type=handle.mime_type,
        headers=headers,
    )


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
        "Content-Disposition": f'attachment; filename="{archive_name}"',
        "X-Folder-Id": folder_id,
        "X-Member-Count": str(len(plan)),
    }
    return StreamingResponse(
        _zip_stream(),
        media_type="application/zip",
        headers=headers,
    )
