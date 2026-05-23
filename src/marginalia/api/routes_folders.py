"""Folder browse + user-side mutation routes."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from marginalia.db.models import FileEntry
from marginalia.db.session import get_session
from marginalia.services import folders as folder_service

router = APIRouter(prefix="/folders", tags=["folders"])


class PatchFolderBody(BaseModel):
    """If `name` is None it is left unchanged. If `parent_id` is the string
    'root' the folder is moved to root (parent_id=NULL). Otherwise it is
    treated as the target id. Omit the field to leave it unchanged."""
    name: str | None = None
    parent_id: str | None = Field(default=None)
    update_parent: bool = False  # set true to actually act on parent_id


def _serialize_folder(folder: Any) -> dict[str, Any]:
    return {
        "id": folder.id,
        "parent_id": folder.parent_id,
        "name": folder.name,
        "created_at": folder.created_at.isoformat() if folder.created_at else None,
        "updated_at": folder.updated_at.isoformat() if folder.updated_at else None,
    }


def _serialize_entry(e: FileEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "folder_id": e.folder_id,
        "file_id": e.file_id,
        "display_name": e.display_name,
        "lifecycle": e.lifecycle,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("")
async def list_folders(
    parent_id: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if parent_id is None:
        rows = await folder_service.list_root_folders(session)
    else:
        rows = await folder_service.list_child_folders(session, parent_id)
    return {"folders": [_serialize_folder(f) for f in rows]}


@router.get("/{folder_id}")
async def get_folder(
    folder_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    folder = await folder_service.get_folder(session, folder_id)
    if folder is None:
        raise HTTPException(status_code=404, detail="folder not found")
    children = await folder_service.list_child_folders(session, folder.id)
    entries = (
        await session.execute(
            select(FileEntry)
            .where(FileEntry.folder_id == folder.id, FileEntry.deleted_at.is_(None))
            .order_by(FileEntry.display_name)
        )
    ).scalars().all()
    return {
        **_serialize_folder(folder),
        "children": [_serialize_folder(c) for c in children],
        "entries": [_serialize_entry(e) for e in entries],
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
