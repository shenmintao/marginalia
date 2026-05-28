from __future__ import annotations

from types import SimpleNamespace

import pytest

from marginalia.config import LlmProfile
from marginalia.llm.anthropic_adapter import AnthropicChatClient
from marginalia.llm.factory import _UsageRecordingChatClient
from marginalia.llm.openai_adapter import OpenAIChatClient
from marginalia.llm.types import ChatMessage, ChatRequest, ChatResponse, TokenUsage


@pytest.mark.asyncio
async def test_ingest_profile_disables_thinking_by_default() -> None:
    seen: list[ChatRequest] = []

    class FakeInner:
        profile_name = "ingest"
        provider = "openai-compatible"
        model = "fake"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            seen.append(request)
            return ChatResponse(
                text="ok",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    wrapped = _UsageRecordingChatClient(
        FakeInner(),
        disable_thinking_by_default=True,
    )
    await wrapped.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="index this")],
        max_tokens=32,
    ))

    assert seen[0].extra_body == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_ingest_profile_preserves_explicit_thinking_options() -> None:
    seen: list[ChatRequest] = []

    class FakeInner:
        profile_name = "ingest"
        provider = "openai-compatible"
        model = "fake"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            seen.append(request)
            return ChatResponse(
                text="ok",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    wrapped = _UsageRecordingChatClient(
        FakeInner(),
        disable_thinking_by_default=True,
    )
    await wrapped.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="index this")],
        max_tokens=32,
        extra_body={"thinking": {"type": "enabled"}},
    ))

    assert seen[0].extra_body == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_openai_adapter_passes_reasoning_controls_and_ignores_reasoning_content() -> None:
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content="final answer",
                    reasoning_content="hidden chain of thought",
                    tool_calls=[],
                ),
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    client = OpenAIChatClient(LlmProfile(
        name="ingest",
        provider="openai-compatible",
        api_key="sk-fake",
        base_url="https://example.invalid",
        model="fake-model",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )

    resp = await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "disabled"}},
    ))

    assert seen["reasoning_effort"] == "high"
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}
    assert resp.text == "final answer"
    assert "hidden" not in (resp.text or "")


@pytest.mark.asyncio
async def test_bailian_qwen_maps_thinking_to_enable_thinking() -> None:
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="ok", tool_calls=[]),
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    client = OpenAIChatClient(LlmProfile(
        name="ingest",
        provider="openai-compatible",
        api_key="sk-fake",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/",
        model="qwen-plus",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )

    await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    ))

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 4096,
    }


@pytest.mark.asyncio
async def test_bailian_disable_thinking_uses_enable_thinking_false() -> None:
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="ok", tool_calls=[]),
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    client = OpenAIChatClient(LlmProfile(
        name="ingest",
        provider="openai-compatible",
        api_key="sk-fake",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1/",
        model="deepseek-v4-pro",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )

    await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        reasoning_effort="xhigh",
        extra_body={"thinking": {"type": "disabled"}},
    ))

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {
        "enable_thinking": False,
        "preserve_thinking": False,
    }


@pytest.mark.asyncio
async def test_anthropic_adapter_maps_thinking_from_extra_body() -> None:
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="final answer")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )

    client = AnthropicChatClient(LlmProfile(
        name="ingest",
        provider="anthropic",
        api_key="sk-fake",
        base_url=None,
        model="fake-model",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(create=fake_create),
    )

    resp = await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        extra_body={
            "thinking": {"type": "disabled"},
            "custom": {"x": 1},
        },
    ))

    assert seen["thinking"] == {"type": "disabled"}
    assert seen["extra_body"] == {"custom": {"x": 1}}
    assert resp.text == "final answer"


@pytest.mark.asyncio
async def test_anthropic_adapter_maps_reasoning_effort_to_budget_tokens() -> None:
    seen: dict = {}

    async def fake_create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="final answer")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=3, output_tokens=2),
        )

    client = AnthropicChatClient(LlmProfile(
        name="ingest",
        provider="anthropic",
        api_key="sk-fake",
        base_url=None,
        model="claude-sonnet-4",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        messages=SimpleNamespace(create=fake_create),
    )

    await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=4096,
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    ))

    assert seen["thinking"] == {
        "type": "enabled",
        "budget_tokens": 4095,
    }
