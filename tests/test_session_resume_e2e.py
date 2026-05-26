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

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_session_resume_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, Conversation, Session
from marginalia.llm.types import (
    ChatRequest, ChatResponse, TokenUsage, ToolUseBlock, ToolResultBlock,
)
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

    # The plan request must carry just the new user message — plan stays
    # history-free.
    plan_req = chat.requests[0]
    assert len(plan_req.messages) == 1, len(plan_req.messages)
    assert plan_req.messages[0].content == "第三轮：再总结一下要点。"

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

    # Tool-call replay: an assistant ToolUseBlock pairs with a user
    # ToolResultBlock and ids match.
    tool_use_ids: list[str] = []
    tool_result_ids: list[str] = []
    for m in exec_req.messages:
        if isinstance(m.content, list):
            for blk in m.content:
                if isinstance(blk, ToolUseBlock):
                    tool_use_ids.append(blk.id)
                elif isinstance(blk, ToolResultBlock):
                    tool_result_ids.append(blk.tool_call_id)
    assert tool_use_ids, "expected at least one ToolUseBlock from resumed history"
    assert tool_use_ids == tool_result_ids, (
        f"tool_use vs tool_result ids drifted: {tool_use_ids} vs {tool_result_ids}"
    )

    # Boundary note appears as a user-role message between the resumed
    # arc and the current question.
    boundary_idx = None
    for i, c in enumerate(contents):
        if isinstance(c, str) and "本会话之前已完成的回合回放" in c:
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
        isinstance(c, str) and "本会话之前已完成的回合回放" in c
        for c in contents
    )
    assert not boundary_present, (
        "fresh session must not carry a resume boundary note"
    )
    print("[2] fresh session: no resume prefix injected")


async def main() -> None:
    await _create_schema()
    await test_resume_replays_history()
    await test_fresh_session_no_resume_prefix()
    print("\nALL SESSION-RESUME TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
