"""Transcript replay must rewrite raw `entry_id=<uuid>` footnotes.

The persisted `agent_response` keeps the raw form on purpose (exports
parse it). For the chat sidebar reload, GET /v1/sessions/{id}/messages
should return the human-readable form: `[name](entry:<uuid>)`.

Run:
    .venv/Scripts/python -m pytest tests/test_session_messages_e2e.py -x -q
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_session_messages_e2e_data"
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
from marginalia.db.models import (
    Base, Conversation, File, FileEntry, Folder, Session,
)
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="papers",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()
        f = File(id=new_id(), storage_key=new_id(), sha256="d"*64,
                 size_bytes=10, mime_type="text/plain",
                 original_ext=".md", kind="text",
                 summary="Raft note", description={"sections": []},
                 extra=None, ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f); await s.flush()
        entry = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                          display_name="raft.md", lifecycle="active",
                          catalog_id=None, extra=None,
                          created_at=now, updated_at=now)
        s.add(entry); await s.flush()

        sess = Session(
            id=new_id(), started_at=now, ended_at=None, end_reason=None,
            initiating_user_message="raft?", turn_count=1,
            total_input_tokens=0, total_output_tokens=0, total_cache_read=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(sess); await s.flush()

        agent_response = (
            "Raft 用 leader election 来达成一致[^a]。\n\n"
            f"[^a]: entry_id={entry.id}, section_id=s2 - 这一段写的就是选举\n"
        )
        conv = Conversation(
            id=new_id(), session_id=sess.id, turn_index=0,
            started_at=now, ended_at=_now(),
            user_message="raft?", agent_response=agent_response,
            tool_calls=[],
            llm_calls=[{
                "phase": "plan",
                "model": "fake",
                "plan_text": "1. Read the Raft note.\nSession name: Raft note",
            }],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(conv)
        await s.commit()
        return {"sid": sess.id, "eid": entry.id, "raw": agent_response}


async def test_transcript_rewrites_entry_id() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/v1/sessions/{seeded['sid']}/messages")
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body["turns"]) == 1
            ar = body["turns"][0]["agent_response"]
            assert f"[raft.md](entry:{seeded['eid']})" in ar, ar
            assert "entry_id=" not in ar, ar
            plan = body["turns"][0]["plan_text"]
            assert plan == "Read the Raft note.", plan
            print("[1] transcript rewrites raw entry_id to [name](entry:<uuid>)")


async def main() -> None:
    await _create_schema()
    await test_transcript_rewrites_entry_id()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
