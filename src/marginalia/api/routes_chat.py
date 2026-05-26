"""Chat HTTP route — DESIGN.md §12.2 / plan §5.5.

  POST /chat/{session_id}      — run one user turn as SSE event stream

The agent runtime (`marginalia.agent.runtime.run_turn`) is an async
generator yielding AgentEvent frames. This route wraps it as a proper
text/event-stream response. Each frame becomes one SSE event with
`event:` set to event_type and `data:` carrying the payload.

Event types (see AgentEvent docstring): conversation / planning / plan /
thinking / tool_call / tool_result / answer / error / done.

reflect_turn is enqueued by run_turn at finalize time, before the `done`
event is yielded — there's no separate end-of-turn hook.
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from marginalia.agent.runtime import run_turn
from marginalia.agent.types import AgentTurnError
from marginalia.db.models import Session as SessionRow
from marginalia.db.session import get_session

router = APIRouter(tags=["chat"])


class ChatBody(BaseModel):
    query: str


@router.post("/chat/{session_id}")
async def post_chat(
    session_id: str,
    body: ChatBody,
    db: AsyncSession = Depends(get_session),
) -> Any:
    s = await db.get(SessionRow, session_id)
    if s is None or s.deleted_at is not None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is not None:
        raise HTTPException(status_code=409, detail="session already ended")

    user_message = body.query

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        try:
            async for ev in run_turn(
                session_id=session_id,
                user_message=user_message,
            ):
                yield {"event": ev.event_type, "data": ev.data}
        except AgentTurnError as exc:
            yield {"event": "error", "data": str(exc)}

    return EventSourceResponse(event_stream())
