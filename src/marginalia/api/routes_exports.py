"""Conversation export route — design.md citation/export semantics.

GET /conversations/latest
  → 200 {conversation_id, session_id, started_at, ended_at, user_message_preview}
  Used by the CLI's `/export` (no args) so the user can export the most
  recent finished conversation without remembering its id.

GET /conversations/{conversation_id}/export
  → application/zip stream containing:
      report.md                                 -- the agent_response
      manifest.json                              -- structured citations
      references/<safe_display_name>             -- bytes of each cited entry
      references/<safe_display_name>.metadata.json  -- user-visible metadata

Soft-deleted / missing entries are listed in manifest.missing.
"""
from __future__ import annotations

import io
import json
import zipfile
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Conversation
from marginalia.db.session import get_session
from marginalia.services.exports import (
    ConversationNotFoundError,
    ExportNotReadyError,
    build_export_plan,
    reference_zip_paths,
    render_manifest,
)
from marginalia.storage import get_storage

router = APIRouter(tags=["exports"])

_ZIP_CHUNK = 64 * 1024


@router.get("/conversations/latest")
async def latest_conversation(
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the most-recently-ended conversation.

    Used by the CLI to default `/export` when the user gives no id.
    Returns 404 if no conversation has ended yet.
    """
    row = (
        await session.execute(
            select(Conversation)
            .where(Conversation.ended_at.isnot(None))
            .order_by(Conversation.ended_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no ended conversation found")
    preview = (row.user_message or "").strip()
    if len(preview) > 120:
        preview = preview[:117] + "..."
    return {
        "conversation_id": row.id,
        "session_id": row.session_id,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "user_message_preview": preview,
    }


@router.get("/conversations/{conversation_id}/export")
async def export_conversation(
    conversation_id: str,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    try:
        plan = await build_export_plan(session, conversation_id=conversation_id)
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="conversation not found")
    except ExportNotReadyError as e:
        raise HTTPException(status_code=409, detail=str(e))

    ref_paths = reference_zip_paths(plan)
    manifest = render_manifest(plan)
    metadata_blobs = plan.metadata_blobs

    # Snapshot storage keys so the stream below doesn't depend on the
    # request's DB session (which closes when the dependency exits).
    plan_files: list[tuple[str, str]] = []  # (zip_path, storage_key)
    plan_meta: list[tuple[str, dict]] = []  # (zip_path, metadata_dict)
    for entry_id, (file_path, meta_path) in ref_paths.items():
        cite = next((c for c in plan.citations if c.entry_id == entry_id), None)
        if cite is None or cite.storage_key is None:
            continue
        plan_files.append((file_path, cite.storage_key))
        meta = metadata_blobs.get(entry_id, {"entry_id": entry_id})
        plan_meta.append((meta_path, meta))

    report_md = plan.agent_response or ""
    storage = get_storage()

    archive_name = f"conversation-{conversation_id[:8]}.zip"

    async def _zip_stream() -> AsyncIterator[bytes]:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("report.md", report_md)
            zf.writestr("manifest.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))
            for zip_path, meta_dict in plan_meta:
                zf.writestr(zip_path,
                            json.dumps(meta_dict, ensure_ascii=False, indent=2))
            for zip_path, storage_key in plan_files:
                body = bytearray()
                async for chunk in storage.get(storage_key):
                    body.extend(chunk)
                zf.writestr(zip_path, bytes(body))
        buf.seek(0)
        while True:
            chunk = buf.read(_ZIP_CHUNK)
            if not chunk:
                break
            yield chunk

    headers = {
        "Content-Disposition": f'attachment; filename="{archive_name}"',
        "X-Conversation-Id": conversation_id,
        "X-Citation-Count": str(len(plan.citations)),
        "X-Missing-Count": str(sum(1 for c in plan.citations if c.missing)),
    }
    return StreamingResponse(
        _zip_stream(),
        media_type="application/zip",
        headers=headers,
    )
