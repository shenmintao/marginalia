"""Two concurrent /chat calls on the same session must serialise.

Without per-session serialisation, both turns race on
`latest_turn_index() + 1` and write conversations with the same
turn_index. We assert two complementary guarantees:

  1. The HTTP route layer holds an asyncio.Lock per session so the two
     turns finish with distinct turn_indexes and ordered side-effects
     (turn 0 then turn 1).
  2. As a database-level backstop, `conversations` carries
     UNIQUE(session_id, turn_index) — proven here by attempting to
     INSERT a duplicate row directly and catching IntegrityError.

Run:
    .venv/Scripts/python tests/test_chat_concurrency_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_chat_concurrency_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, Conversation
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.utils.ids import new_id
from marginalia.main import app


async def _create_schema() -> None:
    # Use the real bootstrap so the new UNIQUE index lands in the test DB
    # the same way it does in production.
    await bootstrap_schema()


# ---- fake chat that BLOCKS on a barrier --------------------------------------

class _BarrierChat:
    """Each call sleeps until both concurrent turns are inside the
    plan-phase LLM call, then hands back NO_PLAN responses so the rest
    of the turn is short. The barrier is the "two requests are racing"
    proof — without per-session serialisation, both arrive; with the
    lock, the second one waits for the first to release.

    profile_name / model are read by runtime when persisting llm_calls.
    """
    profile_name = "chat"
    model = "fake-chat"

    def __init__(self) -> None:
        self.calls: list[ChatRequest] = []
        # Fires when the FIRST in-flight call enters .complete().
        self.first_entered = asyncio.Event()
        # Held by the first caller; release lets it return.
        self.release_first = asyncio.Event()
        self._first = True

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls.append(request)
        if self._first:
            self._first = False
            self.first_entered.set()
            # Block long enough that, absent the route lock, the second
            # request would definitely enter .complete() too.
            await self.release_first.wait()
        return ChatResponse(
            text="NO_PLAN: ack",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=10, output_tokens=2),
            parsed_json=None,
        )


def _install(client) -> None:
    import marginalia.agent.runtime as r
    r.get_chat_client = lambda profile="chat": client  # type: ignore[assignment]


# ---- SSE consumer -----------------------------------------------------------

async def _consume_sse(client: httpx.AsyncClient, path: str, body: dict) -> list[dict]:
    events: list[dict] = []
    async with client.stream("POST", path, json=body) as resp:
        assert resp.status_code == 200, await resp.aread()
        event_type = "message"
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if data_lines or event_type != "message":
                    events.append({
                        "event": event_type, "data": "\n".join(data_lines),
                    })
                event_type = "message"
                data_lines = []
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
    return events


# ---- 1. concurrent /chat serialises ------------------------------------------

async def test_concurrent_chat_serialises() -> None:
    chat = _BarrierChat()
    _install(chat)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        resp = await client.post("/v1/sessions", json={
            "initiating_user_message": "concurrency probe",
        })
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]

        async def turn(msg: str) -> list[dict]:
            return await _consume_sse(
                client, f"/v1/chat/{sid}", {"query": msg},
            )

        # Fire two turns at once. Without the lock, both reach
        # .complete() before either finishes, and barrier_chat.calls
        # accumulates two entries before release_first is set.
        t1 = asyncio.create_task(turn("first"))
        t2 = asyncio.create_task(turn("second"))

        # Wait for the first call to enter the LLM stub.
        await asyncio.wait_for(chat.first_entered.wait(), timeout=5.0)

        # Give the event loop ample time to schedule the second request
        # all the way through to its plan-phase .complete() — if the
        # lock is missing, that happens here.
        await asyncio.sleep(0.5)

        in_flight_calls = len(chat.calls)
        chat.release_first.set()

        await asyncio.gather(t1, t2)

    assert in_flight_calls == 1, (
        "expected per-session lock to keep exactly one turn in-flight; "
        f"saw {in_flight_calls} concurrent LLM calls"
    )

    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                select(Conversation.turn_index, Conversation.user_message)
                .where(Conversation.session_id == sid)
                .order_by(Conversation.turn_index)
            )
        ).all()

    assert len(rows) == 2, rows
    indexes = [r[0] for r in rows]
    assert indexes == [0, 1], indexes
    print(f"[1] serialised: turn_indexes={indexes}, "
          f"in_flight_during_first={in_flight_calls}")


# ---- 2. UNIQUE constraint blocks duplicate writes ----------------------------

async def test_unique_constraint_blocks_dupes() -> None:
    """Insert two Conversation rows with the same (session_id, turn_index).
    The DB MUST reject the second one. This is the cross-process backstop
    that protects against the asyncio.Lock not running (multi-worker
    Postgres deploy, scripts that bypass the route layer)."""
    factory = get_session_factory()
    async with factory() as s:
        # Use a fake session_id — UniqueConstraint is on (session_id,
        # turn_index) not on the FK itself, so the insert would be
        # rejected by FK unless we create a session. Easiest path:
        # use a real session id from above by querying.
        sid_row = (
            await s.execute(select(Conversation.session_id).limit(1))
        ).scalar_one()

    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Conversation(
            id=new_id(), session_id=sid_row, turn_index=99,
            started_at=now, user_message="dup-test-1",
            tool_calls=[], llm_calls=[],
        ))
        await s.commit()

    raised = False
    try:
        async with factory() as s:
            s.add(Conversation(
                id=new_id(), session_id=sid_row, turn_index=99,
                started_at=now, user_message="dup-test-2",
                tool_calls=[], llm_calls=[],
            ))
            await s.commit()
    except IntegrityError:
        raised = True
    assert raised, "second INSERT with duplicate (session, turn) was accepted"
    print("[2] UNIQUE(session_id, turn_index) rejects duplicate insert")


async def main() -> None:
    await _create_schema()
    await test_concurrent_chat_serialises()
    await test_unique_constraint_blocks_dupes()
    print("\nALL CHAT-CONCURRENCY TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
