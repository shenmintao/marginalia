"""User-side file_entry mutation routes — design.md §14.1.

  PATCH  /file-entries/{id}     rename / move / change lifecycle
  DELETE /file-entries/{id}     soft-delete + purge_after window

User-only operations: AI never calls these.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import FileEntry
from marginalia.db.session import get_session
from marginalia.services import entries as entry_service
from marginalia.services.upload import (
    DEFAULT_ON_CONFLICT,
    DisplayNameConflictError,
)


router = APIRouter(prefix="/file-entries", tags=["file_entries"])


class PatchEntryBody(BaseModel):
    display_name: str | None = None
    folder_id: str | None = Field(default=None)
    update_folder: bool = False  # set true to actually move
    lifecycle: str | None = None
    on_conflict: Literal["rename", "error", "skip"] = DEFAULT_ON_CONFLICT


def _serialize(e: FileEntry) -> dict[str, Any]:
    return {
        "id": e.id,
        "folder_id": e.folder_id,
        "file_id": e.file_id,
        "display_name": e.display_name,
        "lifecycle": e.lifecycle,
        "deleted_at": e.deleted_at.isoformat() if e.deleted_at else None,
        "purge_after": e.purge_after.isoformat() if e.purge_after else None,
    }


@router.patch("/{entry_id}")
async def patch_entry(
    entry_id: str,
    body: PatchEntryBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        if body.display_name is not None:
            await entry_service.rename_entry(
                session,
                entry_id=entry_id,
                new_name=body.display_name,
                on_conflict=body.on_conflict,
            )
        if body.update_folder:
            if not body.folder_id:
                raise ValueError("folder_id required when update_folder=true")
            await entry_service.move_entry(
                session,
                entry_id=entry_id,
                new_folder_id=body.folder_id,
                on_conflict=body.on_conflict,
            )
        if body.lifecycle is not None:
            await entry_service.change_lifecycle(
                session, entry_id=entry_id, new_lifecycle=body.lifecycle,
            )
    except entry_service.EntryNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="entry not found")
    except DisplayNameConflictError as e:
        await session.rollback()
        raise HTTPException(status_code=409, detail={
            "error": "display_name_conflict",
            "folder_id": e.folder_id, "display_name": e.display_name,
            "existing_entry_id": e.existing_entry_id,
            "existing_file_id": e.existing_file_id,
        })
    except entry_service.InvalidLifecycleTransitionError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(e))

    e = await session.get(FileEntry, entry_id)
    await session.commit()
    if e is None:
        raise HTTPException(status_code=404, detail="entry vanished")
    return _serialize(e)


@router.delete("/{entry_id}", status_code=200)
async def delete_entry(
    entry_id: str,
    purge_after_seconds: int = Query(default=7 * 86400, ge=0, le=365 * 86400),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        e = await entry_service.soft_delete_entry(
            session,
            entry_id=entry_id,
            purge_after_seconds=purge_after_seconds,
        )
    except entry_service.EntryNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="entry not found")
    await session.commit()
    return _serialize(e)
