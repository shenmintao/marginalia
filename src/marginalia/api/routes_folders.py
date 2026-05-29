"""Folder browse + user-side mutation routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import FileEntry
from marginalia.db.session import get_session
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import tasks as tasks_repo
from marginalia.services import folders as folder_service

router = APIRouter(prefix="/folders", tags=["folders"])


class CreateFolderBody(BaseModel):
    """Create an empty folder. parent_id=None means root."""
    parent_id: str | None = None
    name: str


class PatchFolderBody(BaseModel):
    """If `name` is None it is left unchanged. If `parent_id` is the string
    'root' the folder is moved to root (parent_id=NULL). Otherwise it is
    treated as the target id. Omit the field to leave it unchanged."""
    name: str | None = None
    parent_id: str | None = Field(default=None)
    update_parent: bool = False  # set true to actually act on parent_id


def _derive_folder_ingest_status(summary: dict[str, int]) -> str | None:
    if summary.get("total", 0) <= 0:
        return None
    if summary.get("failed", 0) > 0:
        return "failed"
    if summary.get("processing", 0) > 0:
        return "processing"
    if summary.get("pending", 0) > 0:
        return "pending"
    return "done"


def _serialize_ingest_summary(summary: dict[str, int]) -> dict[str, Any]:
    total = int(summary.get("total", 0))
    done = int(summary.get("done", 0))
    return {
        "total": total,
        "pending": int(summary.get("pending", 0)),
        "processing": int(summary.get("processing", 0)),
        "done": done,
        "failed": int(summary.get("failed", 0)),
        "incomplete": max(0, total - done),
        "status": _derive_folder_ingest_status(summary),
    }


def _serialize_folder(
    folder: Any,
    ingest_summary: dict[str, int] | None = None,
) -> dict[str, Any]:
    payload = {
        "id": folder.id,
        "parent_id": folder.parent_id,
        "name": folder.name,
        "created_at": folder.created_at.isoformat() if folder.created_at else None,
        "updated_at": folder.updated_at.isoformat() if folder.updated_at else None,
    }
    if ingest_summary is not None:
        payload["ingest_summary"] = _serialize_ingest_summary(ingest_summary)
    return payload


def _serialize_entry(
    e: FileEntry,
    ingest_status: str | None = None,
    ingest_error: str | None = None,
) -> dict[str, Any]:
    return {
        "id": e.id,
        "folder_id": e.folder_id,
        "file_id": e.file_id,
        "display_name": e.display_name,
        "lifecycle": e.lifecycle,
        "ingest_status": ingest_status,
        "ingest_error": ingest_error,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


async def _serialize_entries_with_errors(
    session: AsyncSession,
    entries: list[tuple[FileEntry, str | None]],
) -> list[dict[str, Any]]:
    failed_file_ids = [e.file_id for e, status in entries if status == "failed"]
    errors = await tasks_repo.latest_failed_ingest_errors_for_files(
        session, failed_file_ids,
    )
    missing = [file_id for file_id in failed_file_ids if file_id not in errors]
    if missing:
        errors.update(
            await audit_events_repo.latest_failed_ingest_reasons_for_files(
                session, missing,
            )
        )
    return [
        _serialize_entry(e, st, errors.get(e.file_id))
        for e, st in entries
    ]


@router.get("")
async def list_folder(
    parent_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if parent_id is None:
        rows = await folder_service.list_root_folders(session)
        entries = await entries_repo.list_live_in_folder(session, None)
    else:
        rows = await folder_service.list_child_folders(session, parent_id)
        entries = await entries_repo.list_live_in_folder(session, parent_id)
    summaries = await folder_service.ingest_summaries_for_subtrees(
        session, [f.id for f in rows],
    )
    return {
        "folders": [_serialize_folder(f, summaries.get(f.id)) for f in rows],
        "entries": await _serialize_entries_with_errors(session, entries),
    }


@router.post("", status_code=201)
async def create_folder(
    body: CreateFolderBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Create an empty folder. The CLI/API style of "folders are path
    side-effects" is preserved for upload, but the GUI needs an explicit
    create-folder action so users can build a classification skeleton
    before placing files."""
    try:
        f = await folder_service.create_folder(
            session, parent_id=body.parent_id, name=body.name,
        )
    except folder_service.FolderNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="parent folder not found")
    except folder_service.FolderNameConflictError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail={
            "error": "folder_name_conflict",
            "name": e.name, "parent_id": e.parent_id,
            "existing_id": e.existing_id,
        })
    except ValueError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    await session.commit()
    return _serialize_folder(f)


@router.get("/{folder_id}")
async def get_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    folder = await folder_service.get_folder(session, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    children = await folder_service.list_child_folders(session, folder.id)
    entries = await entries_repo.list_live_in_folder(session, folder.id)
    summaries = await folder_service.ingest_summaries_for_subtrees(
        session, [folder.id, *[c.id for c in children]],
    )
    return {
        **_serialize_folder(folder, summaries.get(folder.id)),
        "children": [_serialize_folder(c, summaries.get(c.id)) for c in children],
        "entries": await _serialize_entries_with_errors(session, entries),
    }


@router.patch("/{folder_id}")
async def patch_folder(
    folder_id: str,
    body: PatchFolderBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Rename and/or move a folder. Body fields are independent."""
    try:
        if body.name is not None:
            await folder_service.rename_folder(
                session, folder_id=folder_id, new_name=body.name
            )
        if body.update_parent:
            target = body.parent_id if body.parent_id != "root" else None
            await folder_service.move_folder(
                session, folder_id=folder_id, new_parent_id=target,
            )
    except folder_service.FolderNotFoundError as e:
        await session.rollback()
        raise HTTPException(status_code=404, detail=f"folder not found: {e}")
    except folder_service.FolderNameConflictError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail={
            "error": "folder_name_conflict",
            "name": e.name, "parent_id": e.parent_id,
            "existing_id": e.existing_id,
        })
    except ValueError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    f = await folder_service.get_folder(session, folder_id)
    await session.commit()
    return _serialize_folder(f) if f is not None else {"folder_id": folder_id}


@router.delete("/{folder_id}", status_code=200)
async def delete_folder(
    folder_id: str,
    purge_after_seconds: int = Query(default=7 * 86400, ge=0, le=365 * 86400),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        f = await folder_service.soft_delete_folder(
            session,
            folder_id=folder_id,
            purge_after_seconds=purge_after_seconds,
        )
    except folder_service.FolderNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="folder not found")
    await session.commit()
    return {
        "folder_id": f.id,
        "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None,
    }
