"""OpenAI ChatClient adapter (also covers OpenAI-compatible endpoints —
Together, Groq, DeepSeek, local vllm/ollama, etc., via `base_url`).

Notes:
  - OpenAI does its own automatic prefix caching when prompt > ~1024 tokens.
    We don't need to mark cache breakpoints; we DO surface cache hits as
    `cache_read_tokens`. Field name varies by provider:
      OpenAI            -> usage.prompt_tokens_details.cached_tokens
      DeepSeek          -> usage.prompt_cache_hit_tokens (top-level, non-OpenAI)
    The adapter reads DeepSeek's field first, falls back to OpenAI's.
  - OpenAI returns tool-call arguments as JSON STRINGS — we parse to dicts so
    callers see the same shape as Anthropic.
  - Structured output behaviour depends on provider:
      "openai"            -> response_format={"type":"json_schema","strict":true}
                             with the supplied schema (OpenAI proper only).
      "openai-compatible" -> response_format={"type":"json_object"} + the
                             schema rendered as text in the system prompt
                             (DeepSeek / Together / Groq / vllm / ollama
                             don't accept the strict json_schema variant).
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from marginalia.config import LlmProfile
from marginalia.llm.base import AudioClient, ChatClient
from marginalia.llm.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ImageBlock,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
)

log = logging.getLogger(__name__)


_OPENAI_PROVIDERS: tuple[str, ...] = ("openai", "openai-compatible")


class OpenAIChatClient(ChatClient):
    def __init__(self, profile: LlmProfile) -> None:
        if profile.provider not in _OPENAI_PROVIDERS:
            raise ValueError(
                f"profile {profile.name} is not OpenAI-shaped "
                f"(provider={profile.provider!r})"
            )
        self.profile_name = profile.name
        self.model = profile.model
        self._supports_json_schema = profile.provider == "openai"
        self._client = AsyncOpenAI(api_key=profile.api_key, base_url=profile.base_url)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools and request.json_schema:
            raise ValueError("ChatRequest.tools and json_schema are mutually exclusive")

        messages = self._render_messages(request)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
            if request.tool_choice in ("auto", "none", "required"):
                kwargs["tool_choice"] = request.tool_choice
            elif isinstance(request.tool_choice, str):
                kwargs["tool_choice"] = {"type": "function", "function": {"name": request.tool_choice}}

        if request.json_schema is not None:
            schema = request.json_schema
            if self._supports_json_schema:
                name = schema.get("title") or schema.get("name") or "Result"
                kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": name, "schema": schema, "strict": True},
                }
            else:
                kwargs["response_format"] = {"type": "json_object"}
                self._inject_schema_into_system(messages, schema)

        resp = await self._client.chat.completions.create(**kwargs)
        return self._render_response(resp)

    @staticmethod
    def _inject_schema_into_system(messages: list[dict[str, Any]], schema: dict[str, Any]) -> None:
        instruction = (
            "Respond with ONLY a single JSON object that conforms to this JSON Schema. "
            "No prose, no code fences.\n\nSchema:\n" + json.dumps(schema)
        )
        if messages and messages[0].get("role") == "system":
            existing = messages[0].get("content") or ""
            messages[0]["content"] = (existing + "\n\n" if existing else "") + instruction
        else:
            messages.insert(0, {"role": "system", "content": instruction})

    # --- request rendering --------------------------------------------------

    def _render_messages(self, req: ChatRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if req.system:
            out.append({"role": "system", "content": req.system})
        for msg in req.messages:
            out.extend(self._render_message(msg))
        return out

    def _render_message(self, msg: ChatMessage) -> list[dict[str, Any]]:
        if msg.role == "tool":
            blocks = self._coerce_blocks(msg.content)
            results = [b for b in blocks if isinstance(b, ToolResultBlock)]
            return [
                {"role": "tool", "tool_call_id": b.tool_call_id, "content": b.content}
                for b in results
            ]

        if isinstance(msg.content, str):
            return [{"role": msg.role, "content": msg.content}]

        blocks = msg.content
        text_parts = [b for b in blocks if isinstance(b, TextBlock)]
        image_parts = [b for b in blocks if isinstance(b, ImageBlock)]
        tool_uses = [b for b in blocks if isinstance(b, ToolUseBlock)]

        if msg.role == "assistant" and tool_uses:
            text = "".join(p.text for p in text_parts) or None
            return [{
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {
                        "id": t.id,
                        "type": "function",
                        "function": {"name": t.name, "arguments": json.dumps(t.arguments)},
                    }
                    for t in tool_uses
                ],
            }]

        if image_parts:
            content = []
            for p in text_parts:
                content.append({"type": "text", "text": p.text})
            for img in image_parts:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{img.media_type};base64,{img.data_b64}"},
                })
            return [{"role": msg.role, "content": content}]

        # Send each TextBlock as a separate content item so prefix-caching
        # providers (DeepSeek, OpenAI) can cache the stable-prefix block
        # independently from the variable payload block.
        if len(text_parts) > 1:
            content = [{"type": "text", "text": p.text} for p in text_parts]
            return [{"role": msg.role, "content": content}]

        return [{"role": msg.role, "content": "".join(p.text for p in text_parts)}]

    @staticmethod
    def _coerce_blocks(content: str | list[Any]) -> list[Any]:
        if isinstance(content, str):
            return [TextBlock(text=content)]
        return list(content)

    # --- response parsing ---------------------------------------------------

    def _render_response(self, resp: Any) -> ChatResponse:
        choice = resp.choices[0]
        msg = choice.message
        finish = choice.finish_reason or "stop"
        stop_reason: StopReason = {
            "stop": "end_turn",
            "length": "max_tokens",
            "tool_calls": "tool_use",
            "content_filter": "other",
            "function_call": "tool_use",
        }.get(finish, "other")

        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "")
            except json.JSONDecodeError:
                log.warning("OpenAI returned non-JSON tool arguments: %r", tc.function.arguments)
                args = {"_raw": tc.function.arguments}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        text = msg.content
        parsed_json = None
        if text and not tool_calls:
            try:
                parsed_json = json.loads(text)
            except json.JSONDecodeError:
                parsed_json = None

        usage_obj = getattr(resp, "usage", None)
        cache_read = 0
        if usage_obj is not None:
            # DeepSeek surfaces cache hits as a top-level `prompt_cache_hit_tokens`
            # (non-OpenAI extension). OpenAI uses prompt_tokens_details.cached_tokens.
            # Try DeepSeek first since it's only set when present; fall back to OpenAI.
            cache_read = getattr(usage_obj, "prompt_cache_hit_tokens", 0) or 0
            if not cache_read:
                details = getattr(usage_obj, "prompt_tokens_details", None)
                if details is not None:
                    cache_read = getattr(details, "cached_tokens", 0) or 0
                else:
                    # SDK may expose usage as a model_dump-style mapping when the
                    # field isn't in the typed schema; reach in via __dict__/get.
                    raw = getattr(usage_obj, "model_extra", None) or {}
                    cache_read = (
                        raw.get("prompt_cache_hit_tokens")
                        or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
                        or 0
                    )
        usage = TokenUsage(
            input_tokens=getattr(usage_obj, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "completion_tokens", 0) or 0,
            cache_read_tokens=cache_read,
            cache_creation_tokens=0,
        )

        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            parsed_json=parsed_json,
            raw_provider_response=resp,
        )


class OpenAIAudioClient(AudioClient):
    def __init__(self, profile: LlmProfile) -> None:
        if profile.provider not in _OPENAI_PROVIDERS:
            raise ValueError(
                f"profile {profile.name} is not OpenAI-shaped — audio requires "
                f"an OpenAI-compatible provider (provider={profile.provider!r})"
            )
        self.profile_name = profile.name
        self.model = profile.model
        self._client = AsyncOpenAI(api_key=profile.api_key, base_url=profile.base_url)

    async def transcribe(
        self,
        *,
        audio: AsyncIterator[bytes],
        filename: str,
        content_type: str | None = None,
        language: str | None = None,
    ) -> str:
        buf = bytearray()
        async for chunk in audio:
            buf.extend(chunk)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "file": (filename, bytes(buf), content_type or "audio/mpeg"),
            "response_format": "text",
        }
        if language:
            kwargs["language"] = language
        return await self._client.audio.transcriptions.create(**kwargs)
