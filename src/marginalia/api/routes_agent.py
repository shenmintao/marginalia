"""Session HTTP routes — DESIGN.md §12.2.

  POST /sessions               — open a new session
  POST /sessions/{id}/close    — close a session, return totals
  GET  /sessions               — list sessions (chat sidebar)
  GET  /sessions/{id}/messages — replay turns for a session

Chat (per-turn agent execution) lives in routes_chat.py as
`POST /chat/{session_id}` with SSE streaming.

Sessions are server-managed containers; clients keep a session_id and
post chat turns into it; reflect_turn is enqueued per turn (in
agent.runtime), not on close.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.runtime import _rewrite_footnotes_for_display
from marginalia.agent.runtime import TOOL_RESULT_PREVIEW_LEN
from marginalia.agent import tool_display
from marginalia.db.models import Session as SessionRow
from marginalia.db.session import get_session
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import folders as folders_repo
from marginalia.repositories import sessions as session_service
from marginalia.repositories import tags as tags_repo

router = APIRouter(tags=["sessions"])


class CreateSessionBody(BaseModel):
    initiating_user_message: str | None = None


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody | None = None,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    init = (body.initiating_user_message if body else None) or ""
    s = await session_service.create_session(db, initiating_user_message=init)
    await db.commit()
    return {
        "session_id": s.id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
    }


@router.post("/sessions/{session_id}/close", status_code=200)
async def close_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = await session_service.get_live(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is not None:
        return {"session_id": s.id, "ended_at": s.ended_at.isoformat(),
                "end_reason": s.end_reason}
    closed = await session_service.close_session(
        db, session_id=session_id, end_reason="normal"
    )
    await db.commit()
    return {
        "session_id": closed.id,
        "ended_at": closed.ended_at.isoformat() if closed.ended_at else None,
        "end_reason": closed.end_reason,
        "totals": {
            "turn_count": closed.turn_count,
            "input_tokens": closed.total_input_tokens,
            "output_tokens": closed.total_output_tokens,
            "tool_calls": closed.total_tool_calls,
            "llm_calls": closed.total_llm_calls,
        },
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete a session — hides it from the sidebar but leaves
    `conversations` and `journal` rows intact. Journal is the agent's
    first-class memory across sessions; UI delete must not erase it.

    If the session has never been explicitly closed (`ended_at IS NULL`)
    we auto-close it as part of the delete: in practice the GUI never
    calls /close, so every session in the user's DB looks "active"
    forever otherwise. The trash icon clearly means "I am done with
    this", so closing on the user's behalf is what they want.
    """
    s = await db.get(SessionRow, session_id)
    if s is None or s.deleted_at is not None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is None:
        await session_service.close_session(
            db, session_id=session_id, end_reason="deleted"
        )
    await session_service.soft_delete(db, session_id)
    await audit_events_repo.append(
        db, kind="session_deleted", session_id=session_id, payload={},
    )
    await db.commit()


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List sessions for the chat sidebar, ordered most-recent first.

    Each row carries enough to render a clickable list entry:
    session-title preview, turn count, started_at / ended_at, and the close
    reason if any.
    """
    rows = await session_service.list_sessions(db, limit=limit, offset=offset)

    # Legacy sessions opened before write-side backfill have an empty
    # `initiating_user_message`. Fill those previews from the first
    # conversation's user_message in one batched query.
    needs_fallback = [
        s.id for s in rows if not (s.initiating_user_message or "").strip()
    ]
    fallbacks = await session_service.first_user_messages(db, needs_fallback)

    def _preview(s: SessionRow) -> str:
        text = (s.initiating_user_message or "").strip()
        if not text:
            text = (fallbacks.get(s.id) or "").strip()
        return text[:160]

    return {
        "sessions": [
            {
                "session_id": s.id,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "end_reason": s.end_reason,
                "preview": _preview(s),
                "turn_count": s.turn_count or 0,
                "total_input_tokens": s.total_input_tokens or 0,
                "total_output_tokens": s.total_output_tokens or 0,
                "total_tool_calls": s.total_tool_calls or 0,
            }
            for s in rows
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/sessions/{session_id}/messages")
async def session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Replay the transcript of a session for the GUI.

    Returns turns in turn_index order. Each turn carries the
    user_message, the final agent_response, and a denormalized list of
    tool_calls (name, arguments, optional preview, ok flag, duration).
    The GUI uses this to rebuild the same `Turn[]` shape it builds from
    a live SSE stream — without re-executing anything.

    `plan` is best-effort: planners record their plan_text in
    `llm_calls[*]['extra']['plan_text']` when phase=='plan'. We surface
    it if present.
    """
    s = await session_service.get_live(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")

    convs = await session_service.list_for_session_ordered(db, session_id)

    # Pre-resolve every id referenced by any tool_call in this session
    # so the replay payload mirrors the live SSE shape (each call gets a
    # ready-to-render `display` string). Four batched lookups, regardless
    # of turn count.
    all_eids: set[str] = set()
    all_tids: set[str] = set()
    all_fids: set[str] = set()
    all_cids: set[str] = set()
    for c in convs:
        for tc in c.tool_calls or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("arguments") or {}
            name = tc.get("name") or ""
            all_eids.update(tool_display.collect_entry_ids(name, args))
            all_tids.update(tool_display.collect_tag_ids(name, args))
            all_fids.update(tool_display.collect_folder_ids(name, args))
            all_cids.update(tool_display.collect_catalog_ids(name, args))

    entry_names: dict[str, str] = {}
    tag_names: dict[str, str] = {}
    folder_names: dict[str, str] = {}
    catalog_names: dict[str, str] = {}
    if all_eids:
        rows = await entries_repo.list_live_with_file_by_ids(db, list(all_eids))
        entry_names = {entry.id: entry.display_name for entry, _ in rows}
    if all_tids:
        tag_names = await tags_repo.name_by_ids(db, list(all_tids))
    if all_fids:
        folder_names = await folders_repo.name_by_ids(db, list(all_fids))
    if all_cids:
        catalog_names = await catalogs_repo.name_by_ids(db, list(all_cids))

    turns: list[dict[str, Any]] = []
    for c in convs:
        plan_text: str | None = None
        for call in c.llm_calls or []:
            if isinstance(call, dict) and call.get("phase") == "plan":
                pt = call.get("plan_text") or call.get("extra", {}).get("plan_text")
                if isinstance(pt, str) and pt.strip():
                    plan_text = pt
                    break

        tool_calls = []
        for tc in c.tool_calls or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            display = tool_display.format_tool_call(
                name, args,
                resolver=entry_names.get,
                tag_resolver=tag_names.get,
                folder_resolver=folder_names.get,
                catalog_resolver=catalog_names.get,
            )
            # Mirror the live SSE shape: a one-line preview of the tool's
            # result (truncated to TOOL_RESULT_PREVIEW_LEN), so replayed
            # transcripts show the same expandable result body the user
            # saw during the original turn.
            result = tc.get("result")
            error = tc.get("error")
            if error:
                preview: str | None = f"ERROR: {error}"
            elif result is not None:
                p = tool_display.format_tool_result_preview(name, result)
                if len(p) > TOOL_RESULT_PREVIEW_LEN:
                    p = p[:TOOL_RESULT_PREVIEW_LEN] + "..."
                preview = p
            else:
                preview = None
            tool_calls.append({
                "name": name,
                "arguments": args,
                "display": display,
                "ok": tc.get("error") is None,
                "error": tc.get("error"),
                "duration_ms": tc.get("duration_ms"),
                "preview": preview,
            })

        turns.append({
            "conversation_id": c.id,
            "turn_index": c.turn_index,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
            "user_message": c.user_message,
            "agent_response": (
                await _rewrite_footnotes_for_display(c.agent_response)
                if c.agent_response else c.agent_response
            ),
            "plan_text": plan_text,
            "tool_calls": tool_calls,
            "metrics": {
                "tokens_in": c.total_input_tokens or 0,
                "tokens_out": c.total_output_tokens or 0,
                "cache_read": c.total_cache_read or 0,
                "tool_calls": c.total_tool_calls or 0,
                "llm_calls": c.total_llm_calls or 0,
                "duration_ms": c.total_duration_ms or 0,
            },
        })

    return {
        "session_id": s.id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "end_reason": s.end_reason,
        "turns": turns,
    }
