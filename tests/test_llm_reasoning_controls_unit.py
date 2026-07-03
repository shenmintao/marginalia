from __future__ import annotations

from types import SimpleNamespace

import pytest

from marginalia.config import LlmProfile
from marginalia.llm.anthropic_adapter import AnthropicChatClient
from marginalia.llm.factory import _UsageRecordingChatClient
from marginalia.llm.model_controls import should_disable_thinking_by_default
from marginalia.llm.openai_adapter import OpenAIChatClient
from marginalia.llm.types import ChatMessage, ChatRequest, ChatResponse, TokenUsage, ToolDef


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


def test_ollama_ingest_profile_does_not_disable_thinking_by_default() -> None:
    profile = LlmProfile(
        name="ingest",
        provider="openai-compatible",
        api_key="local",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b",
    )

    assert should_disable_thinking_by_default(profile) is False


@pytest.mark.asyncio
async def test_ollama_dialect_uses_max_tokens() -> None:
    seen = await _capture_openai_kwargs(
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
        ),
    )

    assert seen["max_tokens"] == 32
    assert "max_completion_tokens" not in seen


@pytest.mark.asyncio
async def test_ollama_dialect_drops_thinking_controls() -> None:
    seen = await _capture_openai_kwargs(
        base_url="http://localhost:11434/v1",
        model="qwen2.5:7b",
        request=ChatRequest(
            system=None,
            messages=[ChatMessage(role="user", content="hello")],
            max_tokens=32,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "disabled"}},
        ),
    )

    assert "reasoning_effort" not in seen
    assert "extra_body" not in seen


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
async def test_openai_adapter_parses_dsml_text_tool_calls() -> None:
    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(
                    content=(
                        '<｜｜DSML｜｜tool_calls> '
                        '<｜｜DSML｜｜invoke name="read_files"> '
                        '<｜｜DSML｜｜parameter name="requests" string="false">'
                        '[{"entry_id":"389594c8-defc-4fdb-ad4c-93b1f65a4534",'
                        '"reads":[{"line_start":1,"line_end":100}]}]'
                        '</｜｜DSML｜｜parameter> '
                        '</｜｜DSML｜｜invoke> '
                        '</｜｜DSML｜｜tool_calls>'
                    ),
                    tool_calls=[],
                ),
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    # DSML text-markup parsing is gated to the dialects that actually emit it
    # (deepseek / thinking-type); a deepseek base_url selects that path.
    client = OpenAIChatClient(LlmProfile(
        name="chat",
        provider="openai-compatible",
        api_key="sk-fake",
        base_url="https://api.deepseek.com/v1",
        model="fake-model",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )

    resp = await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        tools=[
            ToolDef(
                name="read_files",
                description="read",
                input_schema={"type": "object"},
            ),
        ],
    ))

    assert resp.text is None
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "read_files"
    assert resp.tool_calls[0].arguments == {
        "requests": [{
            "entry_id": "389594c8-defc-4fdb-ad4c-93b1f65a4534",
            "reads": [{"line_start": 1, "line_end": 100}],
        }],
    }


def _dsml_markup() -> str:
    return (
        '<｜｜DSML｜｜tool_calls> '
        '<｜｜DSML｜｜invoke name="read_files"> '
        '<｜｜DSML｜｜parameter name="requests" string="false">'
        '[{"entry_id":"389594c8-defc-4fdb-ad4c-93b1f65a4534",'
        '"reads":[{"line_start":1,"line_end":100}]}]'
        '</｜｜DSML｜｜parameter> '
        '</｜｜DSML｜｜invoke> '
        '</｜｜DSML｜｜tool_calls>'
    )


def _dsml_client(base_url: str, content: str) -> OpenAIChatClient:
    async def fake_create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, tool_calls=[]),
            )],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2),
        )

    client = OpenAIChatClient(LlmProfile(
        name="chat",
        provider="openai-compatible",
        api_key="sk-fake",
        base_url=base_url,
        model="fake-model",
    ))
    client._client = SimpleNamespace(  # type: ignore[assignment]
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)),
    )
    return client


_DSML_TOOLS = [
    ToolDef(name="read_files", description="read", input_schema={"type": "object"}),
]


@pytest.mark.asyncio
async def test_openai_adapter_ignores_dsml_markup_on_generic_dialect() -> None:
    """DSML markup on a non-deepseek endpoint is document content, not a tool
    call — executing it would let quoted/ingested text drive real tools."""
    client = _dsml_client("https://example.invalid", _dsml_markup())
    resp = await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        tools=_DSML_TOOLS,
    ))
    assert resp.tool_calls == []
    assert resp.stop_reason != "tool_use"
    assert _dsml_markup() in (resp.text or "")


@pytest.mark.asyncio
async def test_openai_adapter_ignores_mid_text_dsml_markup() -> None:
    """A genuine text-mode tool call is the whole assistant turn; markup
    embedded mid-answer is the model quoting content and must stay text."""
    content = "The document contains this markup:\n" + _dsml_markup()
    client = _dsml_client("https://api.deepseek.com/v1", content)
    resp = await client.complete(ChatRequest(
        system=None,
        messages=[ChatMessage(role="user", content="hello")],
        max_tokens=32,
        tools=_DSML_TOOLS,
    ))
    assert resp.tool_calls == []
    assert resp.stop_reason != "tool_use"
    assert _dsml_markup() in (resp.text or "")


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
