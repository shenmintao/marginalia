"""End-to-end agent runtime sanity check.

Run:
    .venv/Scripts/python tests/test_agent_e2e.py

Verifies one full plan-execute turn:
  1. POST /v1/sessions creates a session row
  2. POST /v1/chat/{session_id} runs as SSE event stream:
     - plan phase: 1 LLM call with tools=[]
     - execute phase: 2 LLM calls (search_journal then final answer)
     - tool dispatch records tool_calls JSON + counters
     - reflect_turn task enqueued
     - turn_outcome recorded
  3. POST /v1/sessions/{id}/close rolls totals up

Also verifies the budget-tail "wrap up" nudge logic by directly invoking the
private helper (cheaper than a 11-round LLM stub).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_agent_e2e_data"
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
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, File, FileEntry, Folder, Journal, Session,
)
from marginalia.llm.types import (
    ChatRequest, ChatResponse, TokenUsage, ToolCall,
)
from marginalia.utils.ids import new_id
from marginalia.main import app


CALL_LOG: list[ChatRequest] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _consume_sse(
    client, path: str, *, json: dict | None = None
) -> list[dict]:
    """POST and parse the SSE stream into a list of {event, data} dicts."""
    events: list[dict] = []
    async with client.stream("POST", path, json=json or {}) as resp:
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


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---- fake chat client -------------------------------------------------------

class _ScriptedFakeChat:
    """Returns canned responses in order of `responses`. Each call appends
    the request to CALL_LOG so assertions can inspect it."""

    profile_name = "chat"
    model = "fake-chat"

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = responses
        self._i = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        if self._i >= len(self._responses):
            raise RuntimeError("fake LLM script exhausted")
        r = self._responses[self._i]
        self._i += 1
        return r


def _install(client) -> None:
    import marginalia.agent.runtime as r
    r.get_chat_client = lambda profile="chat": client  # type: ignore[assignment]


# ---- seed -------------------------------------------------------------------

async def _seed():
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        f1 = Folder(id=new_id(), parent_id=None, name="Research",
                    created_at=now, updated_at=now)
        f2 = Folder(id=new_id(), parent_id=None, name="Notes",
                    created_at=now, updated_at=now)
        s.add_all([f1, f2])

        f = File(id=new_id(), storage_key="00/aa/x",
                 sha256="z" * 64, size_bytes=10,
                 mime_type="text/plain", original_ext=".txt", kind="text",
                 summary="Notes on consensus algorithms",
                 description={"sections": []}, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f)
        await s.flush()

        e = FileEntry(id=new_id(), folder_id=f1.id, file_id=f.id,
                      display_name="raft.md", lifecycle="active",
                      catalog_id=None, extra=None,
                      created_at=now, updated_at=now)
        s.add(e)
        await s.flush()

        # A historic session+conversation, so the seeded journal note's FK
        # holds. (journal.conversation_id REFERENCES conversations(id).)
        old_session = Session(
            id=new_id(), started_at=now, ended_at=now, end_reason="normal",
            initiating_user_message="(seed)",
            turn_count=1, total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        )
        s.add(old_session)
        await s.flush()
        old_conv = Conversation(
            id=new_id(), session_id=old_session.id, turn_index=0,
            started_at=now, ended_at=now,
            user_message="(seed)", agent_response="(seed)",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(old_conv)
        await s.flush()

        # An old insight the agent should surface during search_journal.
        # search_journal defaults to kinds=['insight'] now (durable
        # cross-session memory) — see [[journal-tiers]].
        s.add(Journal(
            id=new_id(),
            conversation_id=old_conv.id,
            note="raft 共识 leader 选举",
            entry_ids=[e.id],
            tags=["topic:consensus"],
            source_kind="insight",
            created_at=_now(),
        ))
        await s.commit()
        return {"folder_research": f1.id, "folder_notes": f2.id, "entry_id": e.id}


# ---- main -------------------------------------------------------------------

async def main():
    await _create_schema()
    seeded = await _seed()

    # Script: 1 plan call, 1 execute call (calls search_journal),
    #          1 execute call (final answer, no tool calls).
    script = [
        # plan
        ChatResponse(
            text="计划：先调 search_journal 找已有相关笔记，再综合给出回答。",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=900, output_tokens=120, cache_read_tokens=600),
            parsed_json=None,
        ),
        # execute turn 0: model decides to call search_journal
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="call_1", name="search_journal",
                arguments={"text": "raft", "limit": 5},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=1100, output_tokens=80, cache_read_tokens=900),
            parsed_json=None,
        ),
        # execute turn 1: model gives final answer
        ChatResponse(
            text=(
                "Raft 是 leader-based 一致性算法，关键步骤是 leader 选举与 "
                "log replication[^a]。\n\n[^a]: entry_id=" + seeded["entry_id"]
            ),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1300, output_tokens=140, cache_read_tokens=1100),
            parsed_json=None,
        ),
    ]
    fake = _ScriptedFakeChat(script)
    _install(fake)

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post("/v1/sessions",
                             json={"initiating_user_message": "告诉我 Raft 是什么"})
            assert r.status_code == 201, r.text
            session_id = r.json()["session_id"]
            print("[1] session created:", session_id)

            events = await _consume_sse(
                c, f"/v1/chat/{session_id}", json={"query": "告诉我 Raft 是什么"}
            )
            seq = [ev["event"] for ev in events]
            print("[2] event sequence:", seq)

            # Required event_types in order
            assert "conversation" in seq
            assert "planning" in seq
            assert "plan" in seq
            assert seq.count("thinking") >= 2
            assert seq.count("tool_call") == 1
            assert seq.count("tool_result") == 1
            assert seq.count("answer") == 1
            assert seq[-1] == "done"

            conversation_id = next(ev["data"] for ev in events
                                   if ev["event"] == "conversation")
            answer = next(ev["data"] for ev in events if ev["event"] == "answer")
            done = json.loads(
                next(ev["data"] for ev in events if ev["event"] == "done")
            )
            print("    conversation_id:", conversation_id)
            print("    truncated:", done["truncated"])
            print("    tokens_in/out:", done["tokens_in"], done["tokens_out"])
            print("    answer:", answer[:120])

            assert done["truncated"] is False
            assert "Raft" in answer
            assert done["llm_calls"] == 3
            assert done["tool_calls"] == 1

    # ---- DB-level invariants ----------------------------------------------
    factory = get_session_factory()
    async with factory() as s:
        conv = (await s.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )).scalar_one()
        assert conv.user_message == "告诉我 Raft 是什么"
        assert conv.agent_response and "Raft" in conv.agent_response
        assert conv.ended_at is not None

        # llm_calls JSON: 1 plan + 2 execute
        llm_calls = conv.llm_calls or []
        print("[3] conversation.llm_calls phases:",
              [c["phase"] for c in llm_calls])
        assert len(llm_calls) == 3
        assert llm_calls[0]["phase"] == "plan"
        assert llm_calls[1]["phase"] == "execute"
        assert llm_calls[2]["phase"] == "execute"

        # tool_calls JSON: 1 search_journal call with results
        tcs = conv.tool_calls or []
        print("[3] conversation.tool_calls:", [t["name"] for t in tcs])
        assert len(tcs) == 1
        assert tcs[0]["name"] == "search_journal"
        assert tcs[0]["error"] is None
        assert tcs[0]["result"]["count"] >= 1

        # totals match
        assert conv.total_llm_calls == 3
        assert conv.total_tool_calls == 1

        # reflect_turn task enqueued
        rt = (await s.execute(text(
            "SELECT id FROM tasks WHERE kind='reflect_turn' AND payload LIKE :p"
        ), {"p": f'%"{conv.id}"%'})).scalar_one_or_none()
        assert rt is not None, "reflect_turn task not enqueued"
        print("[4] reflect_turn task enqueued:", rt)

        # task_outcomes summary
        outs = (await s.execute(text(
            "SELECT outcome, detail FROM task_outcomes "
            "WHERE task_kind='run_turn' AND object_id=:c"
        ), {"c": conv.id})).all()
        print("[5] run_turn outcomes:", outs)
        assert len(outs) == 1
        assert outs[0][0] == "applied"

    # ---- close session rolls totals ---------------------------------------
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(f"/v1/sessions/{session_id}/close")
            assert r.status_code == 200, r.text
            closed = r.json()
            print("[6] closed totals:", closed["totals"])
            assert closed["totals"]["turn_count"] == 1
            assert closed["totals"]["llm_calls"] == 3
            assert closed["totals"]["tool_calls"] == 1

    # ---- budget-tail: nudge appears once we enter the last 1/3 -----------
    from marginalia.agent.runtime import _budget_tail
    early = _budget_tail(turn=0, limit=15)   # used+1=1, well below nudge_from=11
    late = _budget_tail(turn=10, limit=15)   # used+1=11 >= 11
    print("[7] tail at turn 0:", early[:60])
    print("[7] tail at turn 10:", late[:80])
    assert "tool rounds used 0" in early
    assert "limit 15" in early
    assert "close to the budget limit" not in early
    assert "tool rounds used 10" in late
    assert "close to the budget limit" in late

    print("\nALL AGENT E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
