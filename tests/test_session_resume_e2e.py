"""Test session resume: full backfill + boundary system-note.

Verifies that when run_turn fires on an existing session with prior
turns, the executor's message tape carries:
  - every prior turn (user + assistant w/ tool_use + tool_result + final)
  - the boundary system-note in Chinese
  - the current user message AFTER the note
  - tool_use_id ↔ tool_result.tool_use_id matched within the request

Same scripted-LLM strategy as test_runtime_guards_e2e.py.

Run:
    .venv/Scripts/python tests/test_session_resume_e2e.py
"""
from __future__ import annotations

import os
from uuid import uuid4
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from httpx import ASGITransport
from sqlalchemy import select

_TEST_PARENT = Path(os.environ.get("MARGINALIA_TEST_TMP", Path(__file__).resolve().parent))
_TEST_ROOT = _TEST_PARENT / f"_session_resume_e2e_data_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, Conversation, Session, Task
from marginalia.db.models.task_outcomes import TaskOutcome
from marginalia.llm.types import (
    ChatRequest, ChatResponse, TokenUsage, ToolUseBlock, ToolResultBlock,
)
from marginalia.main import app
from marginalia.utils.ids import new_id
import marginalia.agent.runtime as runtime


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_session_with_history() -> str:
    """Open a session, drop in two completed turns directly via the ORM
    (no LLM round-trip), so resume can replay them on the third turn."""
    factory = get_session_factory()
    sid = new_id()
    now = _now()
    async with factory() as s:
        s.add(Session(
            id=sid, started_at=now,
            initiating_user_message="t1",
            turn_count=2,
            total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        ))
        await s.flush()
        s.add(Conversation(
            id=new_id(), session_id=sid, turn_index=0,
            started_at=now, ended_at=now,
            user_message="第一轮：搜索关于 raft 的笔记。",
            agent_response="找到了一些 raft 相关条目（详见上次回答）。",
            tool_calls=[{
                "name": "search_metadata",
                "arguments": {"text": "raft", "limit": 5},
                "result": {"hits": [{"id": "abc", "name": "raft.pdf"}]},
                "error": None,
                "duration_ms": 120,
                "at": now.isoformat(),
            }],
            llm_calls=[], total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=1, total_llm_calls=2,
            total_duration_ms=120,
        ))
        s.add(Conversation(
            id=new_id(), session_id=sid, turn_index=1,
            started_at=now, ended_at=now,
            user_message="第二轮：把第一篇打开看看。",
            agent_response="读完了 raft.pdf，要点是 leader election...",
            tool_calls=[],
            llm_calls=[], total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=2,
            total_duration_ms=80,
        ))
        await s.commit()
    return sid


class _ScriptedChat:
    profile_name = "chat"
    model = "fake-chat"

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = responses
        self._i = 0
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self._i >= len(self._responses):
            raise RuntimeError(
                f"fake LLM script exhausted at call #{self._i + 1}"
            )
        r = self._responses[self._i]
        self._i += 1
        return r


def _install_chat(client) -> None:
    runtime.get_chat_client = lambda profile="chat": client  # type: ignore


async def _drive(session_id: str, user_message: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    async for ev in runtime.run_turn(
        session_id=session_id, user_message=user_message,
    ):
        out.append((ev.event_type, ev.data))
    return out


async def _consume_sse(
    client: httpx.AsyncClient,
    path: str,
    json_body: dict,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    async with client.stream("POST", path, json=json_body) as resp:
        assert resp.status_code == 200, await resp.aread()
        event_type = "message"
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if data_lines or event_type != "message":
                    out.append((event_type, "\n".join(data_lines)))
                event_type = "message"
                data_lines = []
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
    return out


async def test_resume_replays_history() -> None:
    sid = await _seed_session_with_history()
    chat = _ScriptedChat([
        # plan
        ChatResponse(
            text="1. 在执行阶段直接基于已有上下文回答。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        # execute 0: final answer (no tool_calls)
        ChatResponse(
            text="基于前两轮已有信息：raft 的核心是 leader election。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=900, output_tokens=30),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "第三轮：再总结一下要点。")
    answer = next((d for ev, d in events if ev == "answer"), None)
    assert answer and "raft" in answer, answer

    # The plan request gets lightweight prior-turn context so short follow-ups
    # such as "continue" can be scoped before execute. It should not replay
    # full tool-use blocks; that remains execute-only.
    plan_req = chat.requests[0]
    assert plan_req.cache_breakpoints == [0]
    assert plan_req.messages[-1].content == "第三轮：再总结一下要点。"
    plan_contents = [m.content for m in plan_req.messages]
    assert any(
        isinstance(c, str) and "raft" in c
        for c in plan_contents
    ), "plan request should include prior session context"
    assert any(
        isinstance(c, str) and "leader election" in c
        for c in plan_contents
    ), "plan request should include prior assistant answer"
    boundary_idx_plan = next(
        (
            i for i, content in enumerate(plan_contents)
            if isinstance(content, str)
            and "lightweight prior turns from this same session" in content
        ),
        None,
    )
    assert boundary_idx_plan is not None, "plan request missed history boundary"
    assert any(
        isinstance(content, str)
        and "第三轮：再总结一下要点。" in content
        for content in plan_contents[boundary_idx_plan + 1:]
    ), "live follow-up must appear after planner history"
    assert not any(
        isinstance(blk, (ToolUseBlock, ToolResultBlock))
        for message in plan_req.messages
        if isinstance(message.content, list)
        for blk in message.content
    ), "plan history should not replay tool blocks"

    # The execute request must contain replayed history + boundary note +
    # current user message + plan-echo assistant.
    exec_req = chat.requests[1]
    roles = [m.role for m in exec_req.messages]
    contents = [m.content for m in exec_req.messages]

    # Look for the original turn-1 user_message in the replayed prefix.
    found_t1 = any(
        isinstance(c, str) and "第一轮：搜索关于 raft 的笔记。" in c
        for c in contents
    )
    assert found_t1, f"resumed history missing turn-1 user message; roles={roles}"

    # Tool-call replay: an assistant ToolUseBlock pairs with a tool-role
    # ToolResultBlock and ids match.
    tool_use_ids: list[str] = []
    tool_result_ids: list[str] = []
    tool_result_roles: list[str] = []
    for m in exec_req.messages:
        if isinstance(m.content, list):
            for blk in m.content:
                if isinstance(blk, ToolUseBlock):
                    tool_use_ids.append(blk.id)
                elif isinstance(blk, ToolResultBlock):
                    tool_result_ids.append(blk.tool_call_id)
                    tool_result_roles.append(m.role)
    assert tool_use_ids, "expected at least one ToolUseBlock from resumed history"
    assert tool_use_ids == tool_result_ids, (
        f"tool_use vs tool_result ids drifted: {tool_use_ids} vs {tool_result_ids}"
    )
    assert tool_result_roles and all(role == "tool" for role in tool_result_roles), (
        f"resumed tool results must use role='tool', got {tool_result_roles}"
    )

    # Boundary note appears as a user-role message between the resumed
    # arc and the current question.
    boundary_idx = None
    for i, c in enumerate(contents):
        if isinstance(c, str) and "replay earlier completed turns" in c:
            boundary_idx = i
            break
    assert boundary_idx is not None, "missing resume boundary note"

    # Current turn's user message appears AFTER the boundary note.
    after = [
        c for c in contents[boundary_idx + 1:]
        if isinstance(c, str) and "第三轮：再总结一下要点。" in c
    ]
    assert after, "current user message must follow the boundary note"

    print(
        f"[1] resume replay: prefix carries turn-1 + tool_use/result pair, "
        f"boundary at idx={boundary_idx}, current msg follows."
    )


async def test_fresh_session_no_resume_prefix() -> None:
    """First turn of a brand-new session must NOT inject a boundary note
    (history is empty) — keeps the first-turn cache prefix identical to
    the pre-resume baseline."""
    factory = get_session_factory()
    sid = new_id()
    now = _now()
    async with factory() as s:
        s.add(Session(
            id=sid, started_at=now,
            initiating_user_message="fresh",
            turn_count=0,
            total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        ))
        await s.commit()

    chat = _ScriptedChat([
        ChatResponse(
            text="1. 直接答。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="hello.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)
    await _drive(sid, "你好")

    exec_req = chat.requests[1]
    contents = [m.content for m in exec_req.messages]
    boundary_present = any(
        isinstance(c, str) and "replay earlier completed turns" in c
        for c in contents
    )
    assert not boundary_present, (
        "fresh session must not carry a resume boundary note"
    )
    print("[2] fresh session: no resume prefix injected")


async def test_closed_session_reopens_for_next_turn() -> None:
    """A clicked old session remains the same conversation when continued.

    This mirrors the GUI path: load the transcript for a historical session,
    then send a follow-up to that same session id. The route must reopen the
    session and execute with the prior turns replayed into the LLM request.
    """
    sid = await _seed_session_with_history()
    factory = get_session_factory()
    async with factory() as s:
        session = await s.get(Session, sid)
        assert session is not None
        session.ended_at = _now()
        session.end_reason = "normal"
        await s.commit()

    chat = _ScriptedChat([
        ChatResponse(
            text="1. Continue from stored history.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=10),
            parsed_json=None,
        ),
        ChatResponse(
            text="continued answer",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=200, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        transcript = await c.get(f"/v1/sessions/{sid}/messages")
        assert transcript.status_code == 200, transcript.text
        loaded = transcript.json()
        assert loaded["session_id"] == sid
        assert len(loaded["turns"]) == 2

        events = await _consume_sse(
            c,
            f"/v1/chat/{sid}",
            {"query": "continue tomorrow"},
        )

    assert any(
        event == "answer" and data == "continued answer"
        for event, data in events
    ), events
    async with factory() as s:
        session = await s.get(Session, sid)
        assert session is not None
        assert session.ended_at is None
        assert session.end_reason is None
        assert session.turn_count == 3
        latest = (
            await s.execute(
                select(Conversation)
                .where(Conversation.session_id == sid)
                .order_by(Conversation.turn_index.desc())
            )
        ).scalars().first()
        assert latest is not None
        assert latest.turn_index == 2
        assert latest.user_message == "continue tomorrow"

    exec_req = chat.requests[1]
    contents = [m.content for m in exec_req.messages]
    boundary_idx = next(
        (
            i for i, content in enumerate(contents)
            if isinstance(content, str)
            and "replay earlier completed turns" in content
        ),
        None,
    )
    assert boundary_idx is not None, "continued old session missed resume boundary"
    assert any(
        isinstance(content, str) and "raft" in content
        for content in contents[:boundary_idx]
    ), "continued old session did not replay prior user context"
    assert any(
        isinstance(content, str) and "leader election" in content
        for content in contents[:boundary_idx]
    ), "continued old session did not replay prior assistant context"
    assert any(
        isinstance(content, str) and content == "continue tomorrow"
        for content in contents[boundary_idx + 1:]
    ), "live follow-up must appear after resumed history"

    print("[3] clicked old session reopens, replays history, and appends the next turn")


async def test_empty_execute_response_surfaces_error() -> None:
    sid = await _seed_session_with_history()
    chat = _ScriptedChat([
        ChatResponse(
            text="1. Search the knowledge base for the missing connection.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=900, output_tokens=0),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "third turn that triggers an empty execute")
    errors = [data for event_type, data in events if event_type == "error"]
    answers = [data for event_type, data in events if event_type == "answer"]
    assert errors, events
    assert "returned no answer and no tool calls" in errors[-1]
    assert answers == []

    factory = get_session_factory()
    async with factory() as s:
        conv = (
            await s.execute(
                select(Conversation)
                .where(Conversation.session_id == sid)
                .order_by(Conversation.turn_index.desc())
            )
        ).scalars().first()
        assert conv is not None
        assert conv.agent_response == errors[-1]

        reflect_tasks = (
            await s.execute(select(Task).where(Task.kind == "reflect_turn"))
        ).scalars().all()
        assert all(
            (task.payload or {}).get("conversation_id") != conv.id
            for task in reflect_tasks
        )

        outcome = (
            await s.execute(
                select(TaskOutcome)
                .where(
                    TaskOutcome.task_kind == "run_turn",
                    TaskOutcome.object_id == conv.id,
                )
            )
        ).scalar_one()
        assert outcome.outcome == "error"
        assert outcome.detail["error"] == errors[-1]

    print("[3] empty execute response surfaces an error and skips reflect")


async def test_chat_turn_timeout_finalizes_unfinished_conversation() -> None:
    """Route-level timeout must not leave a replayed turn spinning forever."""
    sid = await _seed_session_with_history()
    import marginalia.api.routes_chat as routes_chat
    from marginalia.repositories import sessions as session_service

    factory = get_session_factory()
    async with factory() as s:
        last = await session_service.latest_turn_index(s, sid)
        turn_index = 0 if last is None else last + 1
        conv = await session_service.start_conversation(
            s,
            session_id=sid,
            turn_index=turn_index,
            user_message="hang forever",
        )
        conversation_id = conv.id
        await s.commit()

    message = routes_chat._timeout_message(0.2)
    await routes_chat._finish_interrupted_turn(
        session_id=sid,
        conversation_id=conversation_id,
        reason="timeout",
        message=message,
    )

    async with factory() as s:
        conv = (
            await s.execute(
                select(Conversation)
                .where(Conversation.session_id == sid)
                .order_by(Conversation.turn_index.desc())
            )
        ).scalars().first()
        assert conv is not None
        assert conv.user_message == "hang forever"
        assert conv.ended_at is not None
        assert conv.agent_response == message
        outcome = (
            await s.execute(
                select(TaskOutcome)
                .where(
                    TaskOutcome.task_kind == "run_turn",
                    TaskOutcome.object_id == conv.id,
                )
            )
        ).scalar_one()
        assert outcome.outcome == "error"
        assert outcome.detail["error"] == message
        assert outcome.detail["interrupted"] == "timeout"

    print("[4] route timeout finalizes the unfinished conversation")


async def main() -> None:
    await _create_schema()
    await test_resume_replays_history()
    await test_fresh_session_no_resume_prefix()
    await test_closed_session_reopens_for_next_turn()
    await test_empty_execute_response_surfaces_error()
    await test_chat_turn_timeout_finalizes_unfinished_conversation()
    print("\nALL SESSION-RESUME TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
