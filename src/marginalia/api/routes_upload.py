"""Upload route: single endpoint, single file, single remote_path.

POST /upload?remote_path=/research/llm/foo.pdf[&on_conflict=rename|error|skip]
  multipart/form-data  field "file"
  → 200 {file_id, entry_id, folder_id, display_name, deduped, auto_renamed, skipped}
  → 409 (on_conflict=error and target name taken)
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.session import get_session
from marginalia.services.folders import AmbiguousRemotePathError
from marginalia.services.upload import (
    DEFAULT_ON_CONFLICT,
    DisplayNameConflictError,
    upload as upload_service,
)
from marginalia.storage import get_storage

router = APIRouter(tags=["upload"])

_DEFAULT_CHUNK = 1024 * 256


async def _stream_uploadfile(uf: UploadFile):
    while True:
        chunk = await uf.read(_DEFAULT_CHUNK)
        if not chunk:
            return
        yield chunk


@router.post("/upload", status_code=201)
async def upload_endpoint(
    remote_path: str = Query(..., description=(
        "Virtual remote path. Four legal forms:\n"
        "  - /a/b/foo.pdf         file→file (display_name = foo.pdf)\n"
        "  - /a/b/                file→folder (display_name = local basename)\n"
        "  - /a/b                 folder OR file (must pass display_name to "
        "disambiguate when last segment has no extension)\n"
        "Use trailing '/' to mark a path as a folder when the last segment "
        "has no extension (e.g. /repos/marginalia/)."
    )),
    display_name: str | None = Query(default=None, description=(
        "Optional override for the entry's display_name. Required when "
        "remote_path's last segment has no extension AND no trailing '/'."
    )),
    on_conflict: Literal["rename", "error", "skip"] = Query(default=DEFAULT_ON_CONFLICT),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    storage = get_storage()
    try:
        result = await upload_service(
            session,
            storage,
            stream=_stream_uploadfile(file),
            fallback_name=file.filename or "upload.bin",
            remote_path=remote_path,
            display_name=display_name,
            content_type=file.content_type,
            on_conflict=on_conflict,
        )
    except AmbiguousRemotePathError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail={
            "error": "ambiguous_remote_path",
            "remote_path": e.remote,
            "hint": "Add trailing '/' for folder, or supply display_name for file.",
        })
    except DisplayNameConflictError as e:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "error": "display_name_conflict",
                "folder_id": e.folder_id,
                "display_name": e.display_name,
                "existing_entry_id": e.existing_entry_id,
                "existing_file_id": e.existing_file_id,
            },
        )
    await session.commit()
    return {
        "file_id": result.file_id,
        "entry_id": result.entry_id,
        "folder_id": result.folder_id,
        "display_name": result.display_name,
        "deduped": result.deduped,
        "auto_renamed": result.auto_renamed,
        "skipped": result.skipped,
    }
