"""LLM-agnostic types used by Marginalia's chat abstraction.

Two providers are supported in V1: OpenAI and Anthropic. Adapters translate
between these dataclasses and each provider's native shapes. Pipelines and
the agent runtime only ever deal with these types.

Design notes:
  - `cache_breakpoints` is a list of message indices where adapters MAY mark
    a prefix as cache-friendly. OpenAI ignores it (caching is automatic).
    Anthropic uses it to place `cache_control` markers.
  - `tools` and `json_schema` are mutually exclusive. If both are supplied,
    adapters must raise.
  - `parsed_json` is set when the response is structured and parsing succeeds.
    On failure, adapters set `text` only and leave `parsed_json=None`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


# ---- content blocks ---------------------------------------------------------

@dataclass(slots=True)
class TextBlock:
    text: str
    kind: Literal["text"] = "text"


@dataclass(slots=True)
class ImageBlock:
    """Base64-encoded image. Adapters translate to the provider's image form."""
    media_type: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data_b64: str
    kind: Literal["image"] = "image"


@dataclass(slots=True)
class ToolUseBlock:
    """Marker that an assistant message carries a tool call. Mostly internal —
    callers usually inspect `ChatResponse.tool_calls` instead."""
    id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["tool_use"] = "tool_use"


@dataclass(slots=True)
class ToolResultBlock:
    tool_call_id: str
    content: str
    is_error: bool = False
    kind: Literal["tool_result"] = "tool_result"


ContentBlock = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


# ---- messages ---------------------------------------------------------------

@dataclass(slots=True)
class ChatMessage:
    """One turn of a conversation.

    Convention:
      - role='user' / 'assistant' carry text + image + tool_use blocks
      - role='tool' carries exactly one ToolResultBlock (translated to the
        provider's tool-result form)
    """
    role: Literal["user", "assistant", "tool"]
    content: str | list[ContentBlock]


# ---- tools ------------------------------------------------------------------

@dataclass(slots=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(slots=True)
class ToolCall:
    """Normalized tool call returned by the model. Both providers' arguments
    are surfaced as already-parsed dicts (OpenAI's JSON-string is parsed by
    the adapter; Anthropic's `input` is passed through)."""
    id: str
    name: str
    arguments: dict[str, Any]


# ---- request / response -----------------------------------------------------

@dataclass(slots=True)
class ChatRequest:
    system: str | None
    messages: list[ChatMessage]
    max_tokens: int
    tools: list[ToolDef] | None = None
    tool_choice: Literal["auto", "none", "required"] | str = "auto"
    json_schema: dict[str, Any] | None = None
    cache_breakpoints: list[int] = field(default_factory=list)
    temperature: float = 0.7
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] | None = None


StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"]


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(slots=True)
class ChatResponse:
    text: str | None
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    usage: TokenUsage
    parsed_json: dict[str, Any] | None = None
    raw_provider_response: Any = None  # debugging only — never persisted
