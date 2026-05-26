"""Soft-delete a session via DELETE /v1/sessions/{id}.

Asserts:
  1. After deleting a closed session, it's hidden from GET /v1/sessions and
     GET /v1/sessions/{id}/messages returns 404.
  2. journal rows tied to that session's conversations survive — agent
     memory is not erased by a UI delete.
  3. Deleting an active session (ended_at IS NULL) returns 422.
  4. Deleting a missing or already-deleted session returns 404.

Run:
    .venv/Scripts/python tests/test_session_delete_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_session_delete_e2e_data"
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

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, Conversation, Journal, Session
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    """Two sessions:
      closed_a — has 1 conversation + 1 journal row, closed (deletable)
      live_b   — open (not deletable while active)
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        a = Session(id=new_id(), started_at=now, ended_at=now, end_reason="normal",
                    initiating_user_message="what is raft?", turn_count=1,
                    total_input_tokens=0, total_output_tokens=0,
                    total_cache_read=0, total_tool_calls=0,
                    total_llm_calls=0, total_duration_ms=0)
        s.add(a); await s.flush()
        ca = Conversation(
            id=new_id(), session_id=a.id, turn_index=0,
            started_at=now, ended_at=now,
            user_message="what is raft?", agent_response="a consensus algorithm",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(ca); await s.flush()
        j = Journal(
            id=new_id(), conversation_id=ca.id,
            note="user asked about raft; cited paxos paper",
            entry_ids=[], tags=[], source_kind="reflect_turn", created_at=now,
        )
        s.add(j)

        b = Session(id=new_id(), started_at=now, ended_at=None, end_reason=None,
                    initiating_user_message="hello", turn_count=0,
                    total_input_tokens=0, total_output_tokens=0,
                    total_cache_read=0, total_tool_calls=0,
                    total_llm_calls=0, total_duration_ms=0)
        s.add(b)
        await s.commit()
        return {
            "closed_a": a.id, "live_b": b.id,
            "conv_a": ca.id, "journal_a": j.id,
        }


async def _journal_count_for_conv(conv_id: str) -> int:
    factory = get_session_factory()
    async with factory() as s:
        rows = (await s.execute(
            select(Journal.id).where(Journal.conversation_id == conv_id)
        )).scalars().all()
        return len(rows)


async def test_soft_delete_closed_session() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Both sessions visible before delete.
            r = await c.get("/v1/sessions")
            assert r.status_code == 200
            ids = {row["session_id"] for row in r.json()["sessions"]}
            assert seeded["closed_a"] in ids and seeded["live_b"] in ids

            # Delete closed session → 204
            r = await c.delete(f"/v1/sessions/{seeded['closed_a']}")
            assert r.status_code == 204, r.text

            # Hidden from list
            r = await c.get("/v1/sessions")
            ids = {row["session_id"] for row in r.json()["sessions"]}
            assert seeded["closed_a"] not in ids
            assert seeded["live_b"] in ids
            print("[1] deleted session hidden from GET /v1/sessions")

            # Replay returns 404
            r = await c.get(f"/v1/sessions/{seeded['closed_a']}/messages")
            assert r.status_code == 404
            print("[2] GET /messages on deleted session → 404")

            # Journal row survived
            assert await _journal_count_for_conv(seeded["conv_a"]) == 1
            print("[3] journal row preserved after soft-delete")

            # Active session — auto-closed + soft-deleted in one shot.
            # In practice the GUI never calls /close, so every session
            # looks "active" forever; refusing here meant nothing was
            # ever deletable.
            r = await c.delete(f"/v1/sessions/{seeded['live_b']}")
            assert r.status_code == 204, r.text
            r = await c.get("/v1/sessions")
            ids = {row["session_id"] for row in r.json()["sessions"]}
            assert seeded["live_b"] not in ids
            print("[4] DELETE active session auto-closes + soft-deletes")

            # Missing → 404
            r = await c.delete("/v1/sessions/does-not-exist")
            assert r.status_code == 404
            print("[5] DELETE missing session → 404")

            # Already deleted → 404 (idempotent rejection). Re-delete
            # the live_b id which we just deleted above.
            r = await c.delete(f"/v1/sessions/{seeded['live_b']}")
            assert r.status_code == 404
            print("[6] DELETE already-deleted session → 404")


async def main() -> None:
    await _create_schema()
    await test_soft_delete_closed_session()
    print("\nALL SESSION-DELETE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
