from __future__ import annotations

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
    hydrate_entry,
    list_remote_entries,
    pull_latest_metadata,
    read_status,
    test_connection,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_WEBDAV_PUBLISH

router = APIRouter(prefix="/sync/webdav", tags=["webdav_sync"])

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
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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


@router.post("/pull")
async def webdav_pull() -> dict[str, Any]:
    try:
        return await pull_latest_metadata()
    except WebDavConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/remote-entries")
async def webdav_remote_entries(
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    return await list_remote_entries(limit=limit, offset=offset)


@router.post("/hydrate/{entry_id}")
async def webdav_hydrate(entry_id: str) -> dict[str, Any]:
    try:
        return await hydrate_entry(entry_id)
    except WebDavConfigError as exc:
        msg = str(exc)
        status = 404 if "not found" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
