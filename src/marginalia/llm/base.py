"""Adapter protocols for chat / audio / etc. Implementations live in sibling
modules (`openai_adapter.py`, `anthropic_adapter.py`)."""
from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from marginalia.llm.types import ChatRequest, ChatResponse


@runtime_checkable
class ChatClient(Protocol):
    """Provider-agnostic chat surface. Implementations carry a fixed
    (api_key, base_url, model) — call-site only chooses what to send."""

    profile_name: str
    model: str

    async def complete(self, request: ChatRequest) -> ChatResponse: ...


@runtime_checkable
class AudioClient(Protocol):
    """Audio transcription surface. V1 only OpenAI-compatible providers."""

    profile_name: str
    model: str

    async def transcribe(
        self,
        *,
        audio: AsyncIterator[bytes],
        filename: str,
        content_type: str | None = None,
        language: str | None = None,
    ) -> str: ...
