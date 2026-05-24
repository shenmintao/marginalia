"""Session HTTP routes — DESIGN.md §12.2.

  POST /sessions               — open a new session
  POST /sessions/{id}/close    — close a session, return totals

Chat (per-turn agent execution) lives in routes_chat.py as
`POST /chat/{session_id}` with SSE streaming.

Sessions are server-managed containers; clients keep a session_id and
post chat turns into it; reflect_turn is enqueued per turn (in
agent.runtime), not on close.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
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

