"""E2E coverage for the request-level chat mode switch."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_chat_quick_mode_e2e_data"
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

from marginalia.db.engine import get_engine
from marginalia.db.models import Base
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage, ToolCall
from marginalia.main import app
import marginalia.agent.runtime as runtime


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _consume_sse(client, path: str, *, json_body: dict) -> list[dict]:
    events: list[dict] = []
    async with client.stream("POST", path, json=json_body) as resp:
        assert resp.status_code == 200, await resp.aread()
        event_type = "message"
        data_lines: list[str] = []
        async for line in resp.aiter_lines():
            if line == "":
                if data_lines or event_type != "message":
                    events.append({
                        "event": event_type,
                        "data": "\n".join(data_lines),
                    })
                event_type = "message"
                data_lines = []
            elif line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
    return events


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
            raise RuntimeError("fake LLM script exhausted")
        response = self._responses[self._i]
        self._i += 1
        return response


class _CountingTool:
    name = "echo_tool"

    def __init__(self) -> None:
        self.call_count = 0

    async def handler(self, db, ctx, arguments):
        self.call_count += 1
        return {
            "echo": arguments,
            "conversation_id": ctx.conversation_id,
            "at": datetime.now(timezone.utc).isoformat(),
        }


def _install_chat(client: _ScriptedChat) -> None:
    runtime.get_chat_client = lambda profile="chat": client  # type: ignore[assignment]


def _install_tool(tool: _CountingTool) -> None:
    fake_def = {
        "name": tool.name,
        "description": "test echo",
        "input_schema": {"type": "object", "properties": {}},
    }

    class _Reg:
        handler = tool.handler

    runtime.get_tool = lambda name: _Reg if name == tool.name else None  # type: ignore[assignment]
    runtime.all_tool_defs = lambda: [fake_def]  # type: ignore[assignment]


async def test_quick_mode_forces_third_execute_round_to_answer() -> None:
    tool = _CountingTool()
    _install_tool(tool)
    chat = _ScriptedChat([
        ChatResponse(
            text="Find one piece of evidence, then answer.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name=tool.name,
                    arguments={"q": "raft"},
                )
            ],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=150, output_tokens=25),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="call_2",
                    name=tool.name,
                    arguments={"q": "paxos"},
                )
            ],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=180, output_tokens=25),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[
                ToolCall(
                    id="call_3",
                    name=tool.name,
                    arguments={"q": "zab"},
                )
            ],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=190, output_tokens=25),
            parsed_json=None,
        ),
        ChatResponse(
            text="Quick answer from collected evidence.",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=210, output_tokens=30),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            created = await c.post(
                "/v1/sessions",
                json={"initiating_user_message": "quick test"},
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]

            events = await _consume_sse(
                c,
                f"/v1/chat/{session_id}",
                json_body={"query": "answer quickly", "mode": "quick"},
            )

    seq = [event["event"] for event in events]
    assert seq.count("thinking") == 4, seq
    assert seq.count("tool_call") == 3, seq
    assert seq.count("tool_result") == 3, seq
    assert seq.count("answer") == 1, seq
    assert tool.call_count == 3

    thinking = [
        json.loads(event["data"])
        for event in events
        if event["event"] == "thinking"
    ]
    assert [item["limit"] for item in thinking] == [4, 4, 4, 4]
    assert [item["mode"] for item in thinking] == ["quick", "quick", "quick", "quick"]
    assert [item["force_final_answer"] for item in thinking] == [False, False, False, True]

    assert len(chat.requests) == 5
    assert chat.requests[1].tools is not None
    assert chat.requests[1].tool_choice == "auto"
    assert chat.requests[2].tools is not None
    assert chat.requests[2].tool_choice == "auto"
    assert chat.requests[3].tools is not None
    assert chat.requests[3].tool_choice == "auto"
    assert chat.requests[4].tools is None
    assert chat.requests[4].tool_choice == "none"
    assert "Quick mode final execute round" in chat.requests[4].messages[-1].content

    done = json.loads(next(event["data"] for event in events if event["event"] == "done"))
    assert done["llm_calls"] == 5
    assert done["tool_calls"] == 3
    assert done["truncated"] is False
    assert done["mode"] == "quick"
