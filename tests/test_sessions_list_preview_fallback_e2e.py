"""GET /v1/sessions backfills empty preview from first conversation.

Sessions opened before write-side backfill landed have an empty
`initiating_user_message`; the read side falls back to the first
conversation's `user_message` so the chat sidebar never shows
"(empty session)" for a session that actually had a turn.

Run:
    .venv/Scripts/python tests/test_sessions_list_preview_fallback_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_sessions_list_preview_e2e_data"
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

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, Conversation, Session
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_sessions() -> dict:
    """Three sessions:
      A — legacy empty preview, two conversations (turn 0 + turn 1)
      B — modern, initiating_user_message already set
      C — empty preview AND no conversations (true ghost — preview stays empty)
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        # A — empty preview, has conversations
        a = Session(id=new_id(), started_at=now - timedelta(minutes=3),
                    ended_at=None, end_reason=None,
                    initiating_user_message="", turn_count=2,
                    total_input_tokens=0, total_output_tokens=0,
                    total_cache_read=0, total_tool_calls=0,
                    total_llm_calls=0, total_duration_ms=0)
        s.add(a)
        await s.flush()
        # turn 1 first to prove ordering by min(turn_index)
        s.add(Conversation(
            id=new_id(), session_id=a.id, turn_index=1,
            started_at=now, ended_at=_now(),
            user_message="follow up", agent_response="...",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        ))
        s.add(Conversation(
            id=new_id(), session_id=a.id, turn_index=0,
            started_at=now, ended_at=_now(),
            user_message="what is raft?", agent_response="...",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        ))

        # B — modern session, write-side backfill in place
        b = Session(id=new_id(), started_at=now - timedelta(minutes=2),
                    ended_at=None, end_reason=None,
                    initiating_user_message="hello there", turn_count=0,
                    total_input_tokens=0, total_output_tokens=0,
                    total_cache_read=0, total_tool_calls=0,
                    total_llm_calls=0, total_duration_ms=0)
        s.add(b)

        # C — ghost: empty preview, no conversations
        c = Session(id=new_id(), started_at=now - timedelta(minutes=1),
                    ended_at=None, end_reason=None,
                    initiating_user_message="", turn_count=0,
                    total_input_tokens=0, total_output_tokens=0,
                    total_cache_read=0, total_tool_calls=0,
                    total_llm_calls=0, total_duration_ms=0)
        s.add(c)
        await s.commit()
        return {"a": a.id, "b": b.id, "c": c.id}


async def test_preview_fallback() -> None:
    seeded = await _seed_sessions()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/v1/sessions")
            assert r.status_code == 200, r.text
            by_id = {row["session_id"]: row for row in r.json()["sessions"]}

    # A: legacy empty — fallback to turn 0 user_message
    assert by_id[seeded["a"]]["preview"] == "what is raft?", by_id[seeded["a"]]
    # B: modern — preview comes from initiating_user_message untouched
    assert by_id[seeded["b"]]["preview"] == "hello there"
    # C: ghost — no conversations, preview stays empty (no spurious fill)
    assert by_id[seeded["c"]]["preview"] == ""
    print("[1] preview fallback: legacy filled, modern preserved, ghost stays empty")


async def main() -> None:
    await _create_schema()
    await test_preview_fallback()
    print("\nALL PREVIEW-FALLBACK CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
