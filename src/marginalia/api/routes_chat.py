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

## Per-session serialisation

`run_turn` is documented (agent/runtime.py module docstring) as assuming
one in-flight turn per session — concurrent calls race on the
`latest_turn_index() + 1` read-modify-write and silently write two
conversations with the same turn_index.

We enforce that here with a per-session asyncio.Lock, held for the
entire lifetime of the SSE stream. Locks live in a plain dict keyed by
session_id. We don't bother evicting — sessions are coarse and
long-lived (one per UI tab open), and a Lock is ~200 bytes; the
process restarts long before this becomes a memory concern.
(WeakValueDictionary was tried first; it doesn't work because the lock
has no other strong reference between requests, so each call sees a
fresh lock and the serialisation collapses.)

Cross-process safety is the database's job: `conversations` carries
UNIQUE(session_id, turn_index), so a multi-worker Postgres deploy still
fails closed (the second writer hits IntegrityError) instead of
producing duplicate rows.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from marginalia.agent.runtime import run_turn
from marginalia.agent.types import AgentTurnError, ChatMode, RunOptions
from marginalia.db.models import Session as SessionRow
from marginalia.db.session import get_session

router = APIRouter(tags=["chat"])
log = logging.getLogger(__name__)


_SESSION_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    """Return the lock for `session_id`, creating one on first access.

    Single-threaded asyncio loop: get-or-create is race-free without
    any extra synchronisation.
    """
    lock = _SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _SESSION_LOCKS[session_id] = lock
    return lock


class ChatBody(BaseModel):
    query: str
    mode: ChatMode = "auto"


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
    lock = _lock_for(session_id)

    async def event_stream() -> AsyncIterator[dict[str, str]]:
        # Hold the lock for the WHOLE turn — plan + execute + finalize
        # all touch shared per-session state (conversation rows, journal
        # via reflect, session-level counters). Releasing earlier would
        # let a concurrent request see partial state.
        async with lock:
            try:
                async for ev in run_turn(
                    session_id=session_id,
                    user_message=user_message,
                    options=RunOptions(mode=body.mode),
                ):
                    yield {"event": ev.event_type, "data": ev.data}
            except AgentTurnError as exc:
                yield {"event": "error", "data": str(exc)}
            except Exception as exc:
                log.exception("chat turn failed for session %s", session_id)
                yield {"event": "error", "data": str(exc)}

    return EventSourceResponse(event_stream())
