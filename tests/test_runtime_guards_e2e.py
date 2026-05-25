"""Focused tests for the runtime guards added 2026-05-24.

Covers three additions on top of the plan-execute loop:
  1. NO_PLAN fast-path — planner can skip execute by emitting `NO_PLAN: ...`
  2. tool-call dedup — repeat (name, args) returns prior result without
     re-running the handler
  3. doom-loop guard — same key crossing threshold within the rolling
     window appends a STOP nudge to the *current* tool message (no
     mutation of prior messages, so prefix cache stays valid)

Strategy: drive runtime.run_turn against a scripted fake chat client and a
scripted fake tool. Avoids real LLM/HTTP cost while exercising the actual
production code path.

Run:
    .venv/Scripts/python tests/test_runtime_guards_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_runtime_guards_e2e_data"
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
from marginalia.db.models import Base, Session
from marginalia.llm.types import (
    ChatRequest, ChatResponse, TokenUsage, ToolCall,
)
from marginalia.utils.ids import new_id
import marginalia.agent.runtime as runtime
from marginalia.agent import tools as tools_pkg


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _open_session(initiating: str) -> str:
    factory = get_session_factory()
    sid = new_id()
    now = _now()
    async with factory() as s:
        s.add(Session(
            id=sid, started_at=now,
            initiating_user_message=initiating,
            turn_count=0,
            total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
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
                f"fake LLM script exhausted at call #{self._i + 1}; "
                "loop should have stopped earlier"
            )
        r = self._responses[self._i]
        self._i += 1
        return r


def _install_chat(client) -> None:
    runtime.get_chat_client = lambda profile="chat": client  # type: ignore


# ---- fake tool that counts how many times its handler ran ------------------

class _CountingTool:
    """Drop-in replacement for a registered tool. Each .handler() call
    increments .call_count so the test can verify dedup actually skipped
    the handler dispatch."""

    def __init__(self, name: str = "echo_tool") -> None:
        self.name = name
        self.call_count = 0

    async def handler(self, db, ctx, arguments):
        self.call_count += 1
        return {"echo": arguments, "n": self.call_count}


def _install_tool(tool: _CountingTool) -> None:
    """Replace get_tool/all_tool_defs to return our scripted tool only."""
    fake_def = {
        "name": tool.name,
        "description": "test echo",
        "input_schema": {"type": "object", "properties": {}},
    }

    class _Reg:
        handler = tool.handler

    runtime.get_tool = lambda n: _Reg if n == tool.name else None  # type: ignore
    runtime.all_tool_defs = lambda: [fake_def]  # type: ignore


# ---- collectors ------------------------------------------------------------

async def _drive(session_id: str, user_message: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    async for ev in runtime.run_turn(
        session_id=session_id, user_message=user_message,
    ):
        out.append((ev.event_type, ev.data))
    return out


# ---- 1. NO_PLAN fast-path --------------------------------------------------

async def test_no_plan_fast_path() -> None:
    sid = await _open_session("hi")
    chat = _ScriptedChat([
        ChatResponse(
            text="NO_PLAN: 不客气，没什么要我做的就先在这儿待命。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        # No second response scripted — if execute fires, the fake will
        # raise "script exhausted" and the test fails.
    ])
    _install_chat(chat)

    events = await _drive(sid, "谢谢")
    seq = [e[0] for e in events]
    assert "planning" in seq
    assert "plan" in seq
    # No execute phase: no `thinking` event.
    assert "thinking" not in seq, seq
    assert "tool_call" not in seq
    answer = next(d for ev, d in events if ev == "answer")
    assert "不客气" in answer, answer
    # Exactly one LLM call (the plan).
    assert len(chat.requests) == 1, len(chat.requests)
    print("[1] NO_PLAN fast-path: 1 LLM call, no execute")


# ---- 2. tool dedup ---------------------------------------------------------

async def test_tool_dedup() -> None:
    sid = await _open_session("dedup test")
    tool = _CountingTool()
    _install_tool(tool)

    chat = _ScriptedChat([
        # plan
        ChatResponse(
            text="先 echo 看看，再 echo 同一参数（应被 dedup）。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=500, output_tokens=30),
            parsed_json=None,
        ),
        # execute 0: echo({"q":"hi"})
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c1", name="echo_tool", arguments={"q": "hi"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        # execute 1: identical args — should be deduped
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c2", name="echo_tool", arguments={"q": "hi"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=700, output_tokens=20),
            parsed_json=None,
        ),
        # execute 2: final answer
        ChatResponse(
            text="done.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=800, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "走两次同样的工具调用")
    tool_results = [d for ev, d in events if ev == "tool_result"]
    assert len(tool_results) == 2, tool_results
    # First call ran; second was deduped → handler ran exactly once.
    assert tool.call_count == 1, tool.call_count
    # Second tool_result frame should carry the deduped flag.
    assert '"deduped": true' in tool_results[1], tool_results[1]
    print("[2] tool dedup: handler ran 1x for 2 identical calls")


# ---- 3. doom-loop guard ----------------------------------------------------

async def test_doom_loop_nudge() -> None:
    sid = await _open_session("doom test")
    tool = _CountingTool()
    _install_tool(tool)

    # Same name, near-duplicate args (each subtly different so dedup
    # does NOT collapse them) — three calls trip the threshold.
    chat = _ScriptedChat([
        ChatResponse(  # plan
            text="测试 doom-loop。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 0
            text=None,
            tool_calls=[ToolCall(id="c1", name="echo_tool",
                                 arguments={"q": "a"})],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 1
            text=None,
            tool_calls=[ToolCall(id="c2", name="echo_tool",
                                 arguments={"q": "a"})],  # dup → counts in seen
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=550, output_tokens=20),
            parsed_json=None,
        ),
        # By the third call to the same key the doom-loop counter trips.
        ChatResponse(  # execute 2
            text=None,
            tool_calls=[ToolCall(id="c3", name="echo_tool",
                                 arguments={"q": "a"})],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 3: final answer
            text="ok.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=700, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "doom")
    # Inspect the fourth chat request (the one AFTER doom-loop tripped):
    # its messages must contain the STOP nudge text appended to the last
    # tool_result block. Append-only: nothing else mutated.
    last_req = chat.requests[-1]
    nudge_seen = False
    for msg in last_req.messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                content = getattr(block, "content", "") or ""
                if "runtime guard" in content and "陷入循环" in content:
                    nudge_seen = True
    assert nudge_seen, (
        "doom-loop nudge not appended to last tool_result. "
        f"messages={[m.role for m in last_req.messages]}"
    )
    # First message in execute messages (user_message) must be byte-identical
    # to the original — append-only preserves prefix cache.
    first_user = next(m for m in last_req.messages if m.role == "user")
    assert first_user.content == "doom", first_user.content
    print("[3] doom-loop nudge appended; original user msg unchanged")


# ---- 4. canonical args (json.dumps sort_keys) ------------------------------

def test_canonical_args() -> None:
    a = runtime._canonical_args({"a": 1, "b": 2})
    b = runtime._canonical_args({"b": 2, "a": 1})
    assert a == b, (a, b)
    # Distinct values must produce distinct keys.
    c = runtime._canonical_args({"a": 1, "b": 3})
    assert c != a
    print("[4] _canonical_args is order-stable")


async def main() -> None:
    await _create_schema()
    test_canonical_args()
    await test_no_plan_fast_path()
    await test_tool_dedup()
    await test_doom_loop_nudge()
    print("\nALL RUNTIME-GUARD TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
