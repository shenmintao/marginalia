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

from marginalia.db.models import Session as SessionRow
from marginalia.db.session import get_session
from marginalia.repositories import sessions as session_service

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
    s = await db.get(SessionRow, session_id)
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


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List sessions for the chat sidebar, ordered most-recent first.

    Each row carries enough to render a clickable list entry:
    initiating message preview (the first user message), turn count,
    started_at / ended_at, and the close reason if any.
    """
    rows = await session_service.list_sessions(db, limit=limit, offset=offset)
    return {
        "sessions": [
            {
                "session_id": s.id,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "end_reason": s.end_reason,
                "preview": (s.initiating_user_message or "").strip()[:160],
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
    s = await db.get(SessionRow, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")

    convs = await session_service.list_for_session_ordered(db, session_id)

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
            tool_calls.append({
                "name": tc.get("name"),
                "arguments": tc.get("arguments") or {},
                "ok": tc.get("error") is None,
                "error": tc.get("error"),
                "duration_ms": tc.get("duration_ms"),
            })

        turns.append({
            "conversation_id": c.id,
            "turn_index": c.turn_index,
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
            "user_message": c.user_message,
            "agent_response": c.agent_response,
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
