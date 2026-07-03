"""User-side file_entry mutation routes — DESIGN.md §14.1.

Each mutation is its own sub-resource endpoint so the request body
expresses exactly one intent. The previous omnibus `PATCH
/file-entries/{id}` mixed three operations into one body and required
an `update_folder=true` flag to distinguish "set folder_id" from
"didn't mean to". That sentinel was the symptom of overloading.

  PATCH  /file-entries/{id}/name       rename
  PATCH  /file-entries/{id}/folder     move (and possibly auto-rename)
  PATCH  /file-entries/{id}/lifecycle  state change
  DELETE /file-entries/{id}            soft-delete + purge_after window

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
    DisplayNameConflictError,
)


router = APIRouter(prefix="/file-entries", tags=["file_entries"])


class RenameBody(BaseModel):
    display_name: str
    on_conflict: Literal["rename", "error", "skip"] | None = None


class MoveBody(BaseModel):
    # folder_id stays required (no default) but accepts an explicit null,
    # which means "move to the vault root". Omitting the field is a 422 —
    # the intent must be spelled out, per this module's one-intent rule.
    folder_id: str | None = Field(...)
    on_conflict: Literal["rename", "error", "skip"] | None = None


class LifecycleBody(BaseModel):
    lifecycle: str


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


def _conflict_response(e: DisplayNameConflictError) -> HTTPException:
    return HTTPException(status_code=409, detail={
        "error": "display_name_conflict",
        "folder_id": e.folder_id, "display_name": e.display_name,
        "existing_entry_id": e.existing_entry_id,
        "existing_file_id": e.existing_file_id,
    })


async def _finalize(session: AsyncSession, entry_id: str) -> dict[str, Any]:
    e = await session.get(FileEntry, entry_id)
    await session.commit()
    if e is None:
        raise HTTPException(status_code=404, detail="entry vanished")
    return _serialize(e)


@router.patch("/{entry_id}/name")
async def rename_entry_endpoint(
    entry_id: str,
    body: RenameBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await entry_service.rename_entry(
            session, entry_id=entry_id,
            new_name=body.display_name, on_conflict=body.on_conflict,
        )
    except entry_service.EntryNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="entry not found")
    except DisplayNameConflictError as exc:
        await session.rollback()
        raise _conflict_response(exc)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return await _finalize(session, entry_id)


@router.patch("/{entry_id}/folder")
async def move_entry_endpoint(
    entry_id: str,
    body: MoveBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await entry_service.move_entry(
            session, entry_id=entry_id,
            new_folder_id=body.folder_id, on_conflict=body.on_conflict,
        )
    except entry_service.EntryNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="entry not found")
    except DisplayNameConflictError as exc:
        await session.rollback()
        raise _conflict_response(exc)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return await _finalize(session, entry_id)


@router.patch("/{entry_id}/lifecycle")
async def lifecycle_entry_endpoint(
    entry_id: str,
    body: LifecycleBody,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await entry_service.change_lifecycle(
            session, entry_id=entry_id, new_lifecycle=body.lifecycle,
        )
    except entry_service.EntryNotFoundError:
        await session.rollback()
        raise HTTPException(status_code=404, detail="entry not found")
    except entry_service.InvalidLifecycleTransitionError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(exc))
    return await _finalize(session, entry_id)


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
