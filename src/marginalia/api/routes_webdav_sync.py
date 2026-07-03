from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.config import get_settings
from marginalia.db.session import get_session
from marginalia.llm.factory import reset_clients_cache
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.services.config_overlay import (
    OverlayValidationError,
    read_overlay,
    validate_and_normalize,
    write_overlay,
)
from marginalia.services.webdav_sync import (
    WebDavConfigError,
    download_latest,
    download_plan,
    download_selected,
    hydrate_entry,
    publish_selected,
    pull_latest_metadata,
    read_status,
    sync_remote_status,
    test_connection,
    upload_plan,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_WEBDAV_PUBLISH

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sync/webdav", tags=["webdav_sync"])

# Raw exception strings can embed WebDAV URLs (possibly with credentials);
# log them server-side and hand clients a generic message.
_GENERIC_WEBDAV_ERROR = "WebDAV request failed; see server logs"

_CONFIG_FIELDS = {
    "webdav_url",
    "webdav_username",
    "webdav_password",
    "webdav_remote_path",
    "webdav_auto_sync_enabled",
    "webdav_auto_sync_interval_minutes",
}


class WebDavConfigBody(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict)


class WebDavSelectedEntriesBody(BaseModel):
    entry_ids: list[str] = Field(default_factory=list)


@router.get("/status")
async def webdav_status() -> dict[str, Any]:
    return read_status()


@router.put("/config")
async def update_webdav_config(body: WebDavConfigBody) -> dict[str, Any]:
    patch = {k: v for k, v in body.patch.items() if k in _CONFIG_FIELDS}
    unknown = sorted(set(body.patch) - _CONFIG_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown field(s): {', '.join(unknown)}")
    s = get_settings()
    try:
        clean = validate_and_normalize(patch)
    except OverlayValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    merged = read_overlay(s.marginalia_home)
    for k, v in clean.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    write_overlay(s.marginalia_home, merged)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_clients_cache()
    return read_status()


@router.post("/test")
async def webdav_test() -> dict[str, Any]:
    try:
        return await test_connection()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav test failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/remote-status")
async def webdav_remote_status() -> dict[str, Any]:
    try:
        return await sync_remote_status()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav remote-status failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/publish")
async def webdav_publish(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not read_status().get("configured"):
        raise HTTPException(status_code=400, detail="WebDAV sync is not configured")
    task = await enqueue(
        session,
        kind=KIND_WEBDAV_PUBLISH,
        payload={"path": "webdav"},
        dedup_key=KIND_WEBDAV_PUBLISH,
        max_attempts=2,
    )
    if task is not None:
        await audit_events_repo.append(
            session,
            kind="task_enqueued",
            task_id=task.id,
            payload={"kind": KIND_WEBDAV_PUBLISH, "scheduled_by": "webdav_sync"},
        )
    await session.commit()
    return {"ok": True, "task_id": task.id if task is not None else None}


@router.get("/upload-plan")
async def webdav_upload_plan() -> dict[str, Any]:
    try:
        return await upload_plan()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav upload-plan failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/publish-selected")
async def webdav_publish_selected(body: WebDavSelectedEntriesBody) -> dict[str, Any]:
    try:
        return await publish_selected(body.entry_ids)
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav publish-selected failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/pull")
async def webdav_pull() -> dict[str, Any]:
    try:
        return await pull_latest_metadata()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav pull failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.get("/download-plan")
async def webdav_download_plan() -> dict[str, Any]:
    try:
        return await download_plan()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav download-plan failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/download")
async def webdav_download() -> dict[str, Any]:
    try:
        return await download_latest()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav download failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/download-selected")
async def webdav_download_selected(body: WebDavSelectedEntriesBody) -> dict[str, Any]:
    try:
        return await download_selected(body.entry_ids)
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        log.exception("webdav download-selected failed")
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)


@router.post("/hydrate/{entry_id}")
async def webdav_hydrate(entry_id: str) -> dict[str, Any]:
    try:
        return await hydrate_entry(entry_id)
    except WebDavConfigError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    except Exception:
        log.exception("webdav hydrate failed entry_id=%s", entry_id)
        raise HTTPException(status_code=502, detail=_GENERIC_WEBDAV_ERROR)
