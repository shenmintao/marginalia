from __future__ import annotations

from types import SimpleNamespace

import pytest

from marginalia.config import LlmProfile
from marginalia.llm.anthropic_adapter import AnthropicChatClient
from marginalia.llm.factory import _UsageRecordingChatClient
from marginalia.llm.openai_adapter import OpenAIChatClient
from marginalia.llm.types import ChatMessage, ChatRequest, ChatResponse, TokenUsage


async def _capture_openai_kwargs(
    *,
    base_url: str,
    model: str,
    request: ChatRequest,
) -> dict:
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
        base_url=base_url,
        model=model,
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )

    await client.complete(request)
    return seen


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
        extra_body={"thinking": {"type": "enabled"}},
    ))

    assert seen["reasoning_effort"] == "high"
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}}
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
async def test_siliconflow_disable_thinking_uses_enable_thinking_false() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen3-32B",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert seen["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_generic_qwen_disable_uses_enable_thinking_false() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://example.invalid/v1",
        model="qwen-vl-plus",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {"enable_thinking": False}


@pytest.mark.asyncio
async def test_generic_qwen_effort_uses_thinking_budget() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://example.invalid/v1",
        model="qwen3-vl-plus",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="medium",
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {
        "enable_thinking": True,
        "thinking_budget": 2048,
    }


@pytest.mark.asyncio
async def test_openrouter_kimi_maps_thinking_and_gateway_reasoning() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://openrouter.ai/api/v1",
        model="moonshotai/kimi-k2.5",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="medium",
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {
        "thinking": {"type": "enabled"},
        "reasoning": {"effort": "medium"},
    }


@pytest.mark.asyncio
async def test_openrouter_disable_thinking_uses_gateway_disable() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-4o",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert seen["extra_body"] == {
        "reasoning": {"enabled": False, "exclude": True},
    }


@pytest.mark.asyncio
async def test_nvidia_qwen_uses_chat_template_kwargs() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://integrate.api.nvidia.com/v1",
        model="qwen/qwen3-32b",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="high",
        ),
    )

    assert seen["extra_body"] == {
        "chat_template_kwargs": {
            "enable_thinking": True,
            "thinking_budget": 4096,
        },
    }


@pytest.mark.asyncio
async def test_minimax_uses_reasoning_split() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.minimax.io/v1",
        model="MiniMax-M2.7",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert seen["extra_body"] == {"reasoning_split": False}


@pytest.mark.asyncio
async def test_deepseek_v4_uses_thinking_type_controls() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="high",
        ),
    )

    assert seen["reasoning_effort"] == "high"
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_deepseek_none_disables_thinking_without_reasoning_effort() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="none",
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_deepseek_explicit_disable_wins_over_reasoning_effort() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_moonshot_kimi_uses_thinking_without_reasoning_effort() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.moonshot.cn/v1",
        model="kimi-k2.6",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="medium",
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_xiaomi_mimo_uses_thinking_type_controls() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="none",
        ),
    )

    assert "reasoning_effort" not in seen
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_volcengine_uses_thinking_type_controls() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-2-0-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="high",
        ),
    )

    assert seen["reasoning_effort"] == "high"
    assert seen["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_byteplus_minimal_disables_thinking_type_controls() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://ark.ap-southeast.bytepluses.com/api/v3",
        model="doubao-seed-2-0-pro",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="minimal",
        ),
    )

    assert seen["reasoning_effort"] == "minimal"
    assert seen["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_gemini_disable_thinking_uses_google_thinking_config() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-2.5-flash",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert seen["extra_body"] == {
        "google": {"thinking_config": {"thinking_budget": 0}},
    }


@pytest.mark.asyncio
async def test_openai_reasoning_models_omit_temperature() -> None:
    seen = await _capture_openai_kwargs(
        base_url="https://api.openai.com/v1",
        model="gpt-5",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="medium",
        ),
    )

    assert "temperature" not in seen
    assert seen["reasoning_effort"] == "medium"


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
