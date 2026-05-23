"""Agent HTTP routes — design.md §12.2.

Two endpoints:
  POST /sessions               — open a new session
  POST /sessions/{id}/turn     — run one user turn (blocking, returns final answer)

Sessions are server-managed containers; clients keep a session_id cookie /
header so subsequent /turn calls land in the same conversation history.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.runtime import run_turn
from marginalia.agent.types import AgentTurnError
from marginalia.db.models import Session as SessionRow
from marginalia.db.session import get_session
from marginalia.services import sessions as session_service

router = APIRouter(tags=["agent"])


class CreateSessionBody(BaseModel):
    initiating_user_message: str | None = None


class TurnBody(BaseModel):
    user_message: str


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


@router.post("/sessions/{session_id}/turn", status_code=200)
async def post_turn(
    session_id: str,
    body: TurnBody,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = await db.get(SessionRow, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is not None:
        raise HTTPException(status_code=409, detail="session already ended")
    try:
        result = await run_turn(
            session_id=session_id,
            user_message=body.user_message,
        )
    except AgentTurnError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "session_id": result.session_id,
        "conversation_id": result.conversation_id,
        "agent_response": result.agent_response,
        "plan": result.plan_text,
        "truncated": result.truncated,
        "usage": {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "tool_calls": result.usage.tool_calls,
            "llm_calls": result.usage.llm_calls,
            "duration_ms": result.usage.duration_ms,
        },
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
