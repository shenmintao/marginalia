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

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.config import get_settings
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
from marginalia.storage.base import StorageBackend

router = APIRouter(tags=["upload"])
log = logging.getLogger(__name__)

_DEFAULT_CHUNK = 1024 * 256
# Content-Length covers the whole multipart envelope (boundaries + part
# headers), so the up-front check gets a little slack; the streaming byte
# counter below is the authoritative limit.
_MULTIPART_OVERHEAD = 16 * 1024


class _UploadTooLargeError(Exception):
    pass


class _ByteCap:
    """Shared flag between the capped request stream and the storage wrapper."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.exceeded = False


class _SizeCappedStorage:
    """Passthrough that turns an over-limit upload into an error.

    The capped stream stops (rather than raises) once the limit is hit so
    the backend's put() completes normally and returns the object's real
    storage key — the only key the route can learn, since MirrorStorage
    computes its own path inside put(). We then delete that partial object
    and raise before the upload service creates any DB rows.
    """

    def __init__(self, inner: StorageBackend, cap: _ByteCap) -> None:
        self._inner = inner
        self._cap = cap

    @property
    def __class__(self):  # type: ignore[override]
        # upload_service dispatches dedup on isinstance(storage, MirrorStorage);
        # masquerade as the wrapped backend so that check keeps working.
        return type(self._inner)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def put(self, key, stream, **kwargs):
        stored_key = await self._inner.put(key, stream, **kwargs)
        if self._cap.exceeded:
            await self._inner.delete(stored_key)
            raise _UploadTooLargeError(self._cap.limit)
        return stored_key


async def _stream_uploadfile(uf: UploadFile, cap: _ByteCap | None = None):
    seen = 0
    while True:
        chunk = await uf.read(_DEFAULT_CHUNK)
        if not chunk:
            return
        seen += len(chunk)
        if cap is not None and seen > cap.limit:
            cap.exceeded = True
            return
        yield chunk


@router.post("/upload", status_code=201)
async def upload_endpoint(
    request: Request,
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
    max_bytes = get_settings().upload_max_bytes
    cap: _ByteCap | None = None
    if max_bytes:
        content_length = request.headers.get("content-length")
        if (
            content_length
            and content_length.isdigit()
            and int(content_length) > max_bytes + _MULTIPART_OVERHEAD
        ):
            raise HTTPException(status_code=413, detail={
                "error": "upload_too_large",
                "max_bytes": max_bytes,
            })
        cap = _ByteCap(max_bytes)
        storage = _SizeCappedStorage(storage, cap)  # type: ignore[assignment]
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
            stream=_stream_uploadfile(file, cap),
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
    except _UploadTooLargeError:
        await session.rollback()
        log.warning(
            "upload rejected: exceeds upload_max_bytes=%d destination=%s",
            max_bytes,
            destination,
        )
        raise HTTPException(status_code=413, detail={
            "error": "upload_too_large",
            "max_bytes": max_bytes,
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
