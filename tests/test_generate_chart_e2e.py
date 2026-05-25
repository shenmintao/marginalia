"""Side-channel test for generate_chart and the runtime's __user_only__
plumbing.

The contract under test (DESIGN.md §10.4 / B.6):
  1. generate_chart returns a result containing __user_only__ with the
     full Vega-Lite spec.
  2. The runtime emits a `user_artifact` SSE event carrying that payload.
  3. The next LLM request's tool_result content includes the model-facing
     fields (`chart_id`, `caption`, `summary`) but does NOT include the
     `__user_only__` blob — keeping the spec out of the model's context.

Run:
    .venv/Scripts/python tests/test_generate_chart_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_generate_chart_e2e_data"
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
from marginalia.agent.tools.generate_chart import generate_chart, SCHEMA


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _open_session() -> str:
    factory = get_session_factory()
    sid = new_id()
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(Session(
            id=sid, started_at=now,
            initiating_user_message="chart test",
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
                f"fake LLM script exhausted at call #{self._i + 1}"
            )
        r = self._responses[self._i]
        self._i += 1
        return r


def _install_chart_tool() -> None:
    """Register only generate_chart for this scripted run."""
    fake_def = {
        "name": "generate_chart",
        "description": "test chart",
        "input_schema": SCHEMA,
    }

    class _Reg:
        handler = staticmethod(generate_chart)

    runtime.get_tool = lambda n: _Reg if n == "generate_chart" else None  # type: ignore
    runtime.all_tool_defs = lambda: [fake_def]  # type: ignore


# -- 1. tool unit: __user_only__ shape -------------------------------------

async def test_generate_chart_unit() -> None:
    args = {
        "mark": "bar",
        "encoding": {
            "x": {"field": "category", "type": "nominal"},
            "y": {"field": "count", "type": "quantitative"},
        },
        "data": [
            {"category": "a", "count": 10},
            {"category": "b", "count": 7},
        ],
        "title": "Sample",
        "caption": "Counts by category.",
    }
    result = await generate_chart(None, None, args)
    assert result["ok"] is True
    assert result["chart_id"].startswith("ch_"), result["chart_id"]
    assert result["caption"] == "Counts by category."
    assert "__user_only__" in result
    payload = result["__user_only__"]
    assert payload["kind"] == "vega_lite"
    spec = payload["spec"]
    # Spec is sanitized: no `transform`, no `data.url`, our $schema only.
    assert "transform" not in spec
    assert isinstance(spec["data"], dict) and "values" in spec["data"]
    assert "url" not in spec["data"]
    assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")
    # encoding.x carried through; whitelist drops anything not in our
    # field schema (test by passing a forbidden key in the args).
    print("[1] generate_chart unit: spec built, side-channel attached")


async def test_generate_chart_rejects_unknown_field() -> None:
    args = {
        "mark": "bar",
        "encoding": {
            "x": {"field": "missing_col", "type": "nominal"},
            "y": {"field": "count", "type": "quantitative"},
        },
        "data": [{"category": "a", "count": 1}],
        "caption": "x.",
    }
    result = await generate_chart(None, None, args)
    assert result["ok"] is False
    assert "missing_col" in result["error"]
    print("[2] generate_chart rejects encoding field absent from data")


async def test_generate_chart_rejects_overlong_data() -> None:
    big = [{"a": i, "b": i} for i in range(2000)]
    args = {
        "mark": "line",
        "encoding": {
            "x": {"field": "a", "type": "quantitative"},
            "y": {"field": "b", "type": "quantitative"},
        },
        "data": big,
        "caption": "x.",
    }
    result = await generate_chart(None, None, args)
    assert result["ok"] is False
    assert "1000" in result["error"], result["error"]
    print("[3] generate_chart caps data at 1000 rows")


# -- 2. runtime side-channel: spec → SSE, NOT model -------------------------

async def test_runtime_side_channel() -> None:
    sid = await _open_session()
    _install_chart_tool()

    chat = _ScriptedChat([
        # plan
        ChatResponse(
            text="先生成一张图。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        # execute 0: call generate_chart
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c1", name="generate_chart",
                arguments={
                    "mark": "bar",
                    "encoding": {
                        "x": {"field": "k", "type": "nominal"},
                        "y": {"field": "v", "type": "quantitative"},
                    },
                    "data": [
                        {"k": "x", "v": 1},
                        {"k": "y", "v": 2},
                    ],
                    "caption": "示意图。",
                },
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        # execute 1: final answer
        ChatResponse(
            text="见图 [^chart-1].",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
    ])
    runtime.get_chat_client = lambda profile="chat": chat  # type: ignore

    events: list[tuple[str, str]] = []
    async for ev in runtime.run_turn(session_id=sid, user_message="画个图"):
        events.append((ev.event_type, ev.data))

    # 1. user_artifact event was emitted, in the expected order:
    #    tool_call → user_artifact → tool_result → ...
    seq = [e[0] for e in events]
    assert "user_artifact" in seq, seq
    ua_idx = seq.index("user_artifact")
    assert seq[ua_idx - 1] == "tool_call", seq
    assert seq[ua_idx + 1] == "tool_result", seq

    # 2. user_artifact payload carries the spec.
    artifact_data = json.loads(events[ua_idx][1])
    assert artifact_data["tool"] == "generate_chart"
    payload = artifact_data["payload"]
    assert payload["kind"] == "vega_lite"
    assert "spec" in payload
    assert payload["spec"]["mark"] == "bar"
    assert "values" in payload["spec"]["data"]

    # 3. The next LLM request's tool message MUST NOT contain __user_only__
    #    or the raw spec. It only sees chart_id + caption + summary.
    last_req = chat.requests[-1]
    tool_msg = next(m for m in last_req.messages if m.role == "tool")
    assert isinstance(tool_msg.content, list)
    block = tool_msg.content[0]
    body = getattr(block, "content", "")
    assert "__user_only__" not in body, body
    assert '"spec"' not in body, body
    assert "vega-lite" not in body.lower(), body
    # chart_id IS visible to the model.
    assert "chart_id" in body, body
    assert "caption" in body, body
    print("[4] runtime side-channel: artifact emitted, spec hidden from model")


async def main() -> None:
    await _create_schema()
    await test_generate_chart_unit()
    await test_generate_chart_rejects_unknown_field()
    await test_generate_chart_rejects_overlong_data()
    await test_runtime_side_channel()
    print("\nALL GENERATE_CHART TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
