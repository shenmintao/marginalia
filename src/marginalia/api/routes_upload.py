"""Upload route: single endpoint, single file. Two destination styles:

POST /upload?remote_path=/research/llm/foo.pdf[&on_conflict=rename|error|skip]
  CLI/API style: path string, folders auto-created.

POST /upload?folder_id=<id>[&display_name=foo.pdf][&on_conflict=...]
  GUI style: target folder already selected; display_name defaults to local
  filename.

  multipart/form-data  field "file"
  → 200 {file_id, entry_id, folder_id, display_name, deduped, auto_renamed, skipped}
  → 409 (on_conflict=error and target name taken)
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.session import get_session
from marginalia.services.folders import (
    AmbiguousRemotePathError,
    FolderNotFoundError,
)
from marginalia.services.upload import (
    DisplayNameConflictError,
    upload as upload_service,
)
from marginalia.storage import get_storage

router = APIRouter(tags=["upload"])
log = logging.getLogger(__name__)

_DEFAULT_CHUNK = 1024 * 256


async def _stream_uploadfile(uf: UploadFile):
    while True:
        chunk = await uf.read(_DEFAULT_CHUNK)
        if not chunk:
            return
        yield chunk


@router.post("/upload", status_code=201)
async def upload_endpoint(
    remote_path: str | None = Query(default=None, description=(
        "Virtual remote path (mutually exclusive with folder_id). "
        "Folders along the path are auto-created. Four legal forms:\n"
        "  - /a/b/foo.pdf         file→file (display_name = foo.pdf)\n"
        "  - /a/b/                file→folder (display_name = local basename)\n"
        "  - /a/b                 folder OR file (must pass display_name to "
        "disambiguate when last segment has no extension)"
    )),
    folder_id: str | None = Query(default=None, description=(
        "Target folder id (mutually exclusive with remote_path). The folder "
        "must already exist. display_name defaults to the local filename."
    )),
    display_name: str | None = Query(default=None, description=(
        "Optional override for the entry's display_name. Required when "
        "remote_path's last segment has no extension AND no trailing '/'."
    )),
    on_conflict: Literal["rename", "error", "skip"] | None = Query(default=None),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if (remote_path is None) == (folder_id is None):
        raise HTTPException(status_code=400, detail={
            "error": "invalid_destination",
            "hint": "exactly one of remote_path or folder_id is required",
        })
    storage = get_storage()
    destination = "remote_path" if remote_path is not None else "folder_id"
    log.info(
        "upload started destination=%s content_type=%s conflict_policy=%s",
        destination,
        file.content_type,
        on_conflict or "default",
    )
    try:
        result = await upload_service(
            session,
            storage,
            stream=_stream_uploadfile(file),
            fallback_name=file.filename or "upload.bin",
            remote_path=remote_path,
            folder_id=folder_id,
            display_name=display_name,
            content_type=file.content_type,
            on_conflict=on_conflict,
        )
    except FolderNotFoundError:
        await session.rollback()
        log.warning("upload rejected: folder not found destination=%s", destination)
        raise HTTPException(status_code=404, detail="folder not found")
    except AmbiguousRemotePathError as e:
        await session.rollback()
        log.warning("upload rejected: ambiguous remote path")
        raise HTTPException(status_code=400, detail={
            "error": "ambiguous_remote_path",
            "remote_path": e.remote,
            "hint": "Add trailing '/' for folder, or supply display_name for file.",
        })
    except DisplayNameConflictError as e:
        await session.rollback()
        log.warning(
            "upload rejected: display name conflict folder_id=%s existing_entry_id=%s",
            e.folder_id,
            e.existing_entry_id,
        )
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
    except Exception:
        await session.rollback()
        log.exception("upload failed unexpectedly destination=%s", destination)
        raise
    await session.commit()
    log.info(
        "upload completed file_id=%s entry_id=%s folder_id=%s deduped=%s skipped=%s",
        result.file_id,
        result.entry_id,
        result.folder_id,
        result.deduped,
        result.skipped,
    )
    return {
        "file_id": result.file_id,
        "entry_id": result.entry_id,
        "folder_id": result.folder_id,
        "display_name": result.display_name,
        "deduped": result.deduped,
        "auto_renamed": result.auto_renamed,
        "skipped": result.skipped,
    }
