"""Anthropic ChatClient adapter.

Notes:
  - Anthropic requires `max_tokens` (no default).
  - System prompt is a top-level parameter (not a message). It can be a string
    or a list of system blocks; we use the list form so the adapter can place
    a `cache_control` marker on the system block when it is large.
  - Cache breakpoints: we apply `cache_control: {"type": "ephemeral"}` to the
    LAST content block of the message at each requested index. We additionally
    auto-cache the system prompt when its length exceeds AUTO_CACHE_THRESHOLD
    chars. (Anthropic permits up to 4 cache_control markers per request.)
  - JSON mode is not native. When `json_schema` is supplied we append a strict
    "respond ONLY with JSON matching this schema" instruction to system, and
    parse the response. Failure → `parsed_json=None`.
  - Tool calls come back as content blocks of type "tool_use"; we surface them
    as ToolCall objects with arguments already as dicts (no JSON-string parse).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic, BadRequestError

from marginalia.config import LlmProfile
from marginalia.llm.base import ChatClient
from marginalia.llm.model_controls import anthropic_reasoning_controls
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

AUTO_CACHE_THRESHOLD = 1024


class AnthropicChatClient(ChatClient):
    def __init__(self, profile: LlmProfile) -> None:
        if profile.provider != "anthropic":
            raise ValueError(f"profile {profile.name} is not Anthropic")
        self.profile_name = profile.name
        self.provider = profile.provider
        self.model = profile.model
        kwargs: dict[str, Any] = {"api_key": profile.api_key}
        if profile.base_url:
            kwargs["base_url"] = profile.base_url
        self._client = AsyncAnthropic(**kwargs)

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if request.tools and request.json_schema:
            raise ValueError("ChatRequest.tools and json_schema are mutually exclusive")

        messages = self._render_messages(request)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }

        system = self._render_system(request)
        if system is not None:
            kwargs["system"] = system
        thinking, extra_body = anthropic_reasoning_controls(request)
        if thinking:
            kwargs["thinking"] = thinking
        if extra_body:
            kwargs["extra_body"] = extra_body

        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
            if request.tool_choice == "auto":
                kwargs["tool_choice"] = {"type": "auto"}
            elif request.tool_choice == "none":
                kwargs["tool_choice"] = {"type": "none"}
            elif request.tool_choice == "required":
                kwargs["tool_choice"] = {"type": "any"}
            elif isinstance(request.tool_choice, str):
                kwargs["tool_choice"] = {"type": "tool", "name": request.tool_choice}

        try:
            resp = await self._client.messages.create(**kwargs)
        except BadRequestError:
            if not (request.extra_body or request.reasoning_effort):
                raise
            log.warning(
                "provider rejected reasoning controls for profile %s; retrying without them",
                self.profile_name,
            )
            kwargs.pop("thinking", None)
            kwargs.pop("extra_body", None)
            resp = await self._client.messages.create(**kwargs)
        return self._render_response(resp, expected_json=request.json_schema is not None)

    # --- system rendering ---------------------------------------------------

    def _render_system(self, req: ChatRequest) -> str | list[dict[str, Any]] | None:
        sys_text = req.system or ""
        if req.json_schema is not None:
            sys_text = (sys_text + "\n\n" if sys_text else "") + (
                "Respond with ONLY a single JSON object that conforms to this JSON Schema. "
                "No prose, no code fences.\n\nSchema:\n" + json.dumps(req.json_schema)
            )
        if not sys_text:
            return None

        if len(sys_text) >= AUTO_CACHE_THRESHOLD:
            return [{
                "type": "text",
                "text": sys_text,
                "cache_control": {"type": "ephemeral"},
            }]
        return sys_text

    # --- message rendering --------------------------------------------------

    def _render_messages(self, req: ChatRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cache_at = set(req.cache_breakpoints)
        for i, msg in enumerate(req.messages):
            rendered = self._render_message(msg)
            if i in cache_at and rendered.get("content"):
                content = rendered["content"]
                if isinstance(content, list) and content:
                    last = dict(content[-1])
                    last["cache_control"] = {"type": "ephemeral"}
                    rendered["content"] = content[:-1] + [last]
            out.append(rendered)
        return out

    def _render_message(self, msg: ChatMessage) -> dict[str, Any]:
        if msg.role == "tool":
            blocks = self._coerce_blocks(msg.content)
            results = [b for b in blocks if isinstance(b, ToolResultBlock)]
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.content,
                    **({"is_error": True} if b.is_error else {}),
                }
                for b in results
            ]
            return {"role": "user", "content": content}

        if isinstance(msg.content, str):
            return {"role": msg.role, "content": [{"type": "text", "text": msg.content}]}

        content_out: list[dict[str, Any]] = []
        for b in msg.content:
            if isinstance(b, TextBlock):
                content_out.append({"type": "text", "text": b.text})
            elif isinstance(b, ImageBlock):
                content_out.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": b.media_type,
                        "data": b.data_b64,
                    },
                })
            elif isinstance(b, ToolUseBlock):
                content_out.append({
                    "type": "tool_use",
                    "id": b.id,
                    "name": b.name,
                    "input": b.arguments,
                })
            elif isinstance(b, ToolResultBlock):
                content_out.append({
                    "type": "tool_result",
                    "tool_use_id": b.tool_call_id,
                    "content": b.content,
                    **({"is_error": True} if b.is_error else {}),
                })
        return {"role": msg.role, "content": content_out}

    @staticmethod
    def _coerce_blocks(content: str | list[Any]) -> list[Any]:
        if isinstance(content, str):
            return [TextBlock(text=content)]
        return list(content)

    # --- response parsing ---------------------------------------------------

    def _render_response(self, resp: Any, *, expected_json: bool) -> ChatResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in resp.content:
            t = getattr(block, "type", None)
            if t == "text":
                text_parts.append(block.text)
            elif t == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))

        text = "".join(text_parts) if text_parts else None
        stop_reason: StopReason = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
            "stop_sequence": "stop_sequence",
        }.get(getattr(resp, "stop_reason", "") or "", "other")

        parsed_json = None
        if expected_json and text:
            stripped = text.strip()
            if stripped.startswith("```"):
                stripped = stripped.strip("`")
                if stripped.lower().startswith("json"):
                    stripped = stripped[4:].lstrip()
            try:
                parsed_json = json.loads(stripped)
            except json.JSONDecodeError:
                log.warning("Anthropic response not parseable as JSON despite json_schema request")

        usage_obj = getattr(resp, "usage", None)
        usage = TokenUsage(
            input_tokens=getattr(usage_obj, "input_tokens", 0) or 0,
            output_tokens=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage_obj, "cache_read_input_tokens", 0) or 0,
            cache_creation_tokens=getattr(usage_obj, "cache_creation_input_tokens", 0) or 0,
        )

        return ChatResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            parsed_json=parsed_json,
            raw_provider_response=resp,
        )
