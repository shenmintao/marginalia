"""LLM client factories. One profile → one cached client (cheap to create,
but pinning lets adapters share connection pools across calls)."""
from __future__ import annotations

from functools import lru_cache

from marginalia.config import (
    LlmProfile,
    get_settings,
    has_audio_profile,
    has_vision_profile,
    resolve_profile,
)
from marginalia.llm.anthropic_adapter import AnthropicChatClient
from marginalia.llm.base import AudioClient, ChatClient
from marginalia.llm.model_controls import (
    should_disable_thinking_by_default,
    with_disabled_thinking,
)
from marginalia.llm.openai_adapter import OpenAIAudioClient, OpenAIChatClient
from marginalia.llm.types import ChatRequest, ChatResponse


class _UsageRecordingChatClient:
    """Transparent wrapper that drops every response.usage into the task-
    bound UsageCounters (if one is bound). When nothing is bound — e.g.
    chat-page calls running outside the TaskRunner — record_chat_use is a
    no-op and the wrapper is invisible.

    Kept as a thin proxy so `isinstance(c, ChatClient)` still holds and
    test code that monkeypatches `complete` keeps working."""

    def __init__(
        self,
        inner: ChatClient,
        *,
        disable_thinking_by_default: bool = False,
    ) -> None:
        self._inner = inner
        self.profile_name = inner.profile_name
        self.provider = inner.provider
        self.model = inner.model
        self._disable_thinking_by_default = disable_thinking_by_default

    async def complete(self, request: ChatRequest) -> ChatResponse:
        if self._disable_thinking_by_default:
            request = with_disabled_thinking(request)
        # Import here to avoid a circular dep at module import time
        # (tasks.usage doesn't import llm, but tasks.runner does, and
        # tasks.runner imports through this factory).
        from marginalia.tasks.usage import record_chat_use
        resp = await self._inner.complete(request)
        record_chat_use(resp.usage)
        return resp


def _build_chat(profile: LlmProfile) -> ChatClient:
    if profile.provider in ("openai", "openai-compatible"):
        inner: ChatClient = OpenAIChatClient(profile)
    elif profile.provider == "anthropic":
        inner = AnthropicChatClient(profile)
    else:
        raise ValueError(f"unknown provider for profile {profile.name}: {profile.provider}")
    return _UsageRecordingChatClient(
        inner,
        disable_thinking_by_default=should_disable_thinking_by_default(profile),
    )


@lru_cache(maxsize=8)
def get_chat_client(profile: str = "ingest") -> ChatClient:
    """Get a chat client by profile name.

    Profile names:
      - "chat"    → online agent (plan-execute)
      - "reflect" → reflect_turn (strong model + long context)
      - "ingest"  → ingest_file pipelines AND offline batch tasks
                    (enrich_tags / restructure_catalogs / suggest_*)
      - "vision"  → image_pipeline VLM
      - "audio"   → NOT served here; use get_audio_client()
    """
    if profile == "audio":
        raise ValueError("use get_audio_client() for the audio profile")
    settings = get_settings()
    if profile == "vision" and not has_vision_profile(settings):
        # Don't silently fall back to LLM_DEFAULT_*: the default model
        # is usually text-only and the call would fail with a provider
        # 400 per request. Callers should gate on `has_vision_profile`
        # before reaching here.
        raise ValueError(
            "vision profile is not configured; set LLM_VISION_* "
            "(or guard the call with has_vision_profile())"
        )
    p = resolve_profile(settings, profile)
    return _build_chat(p)


@lru_cache(maxsize=2)
def get_audio_client() -> AudioClient:
    settings = get_settings()
    if not has_audio_profile(settings):
        # Same reasoning as vision: a default chat endpoint won't have
        # /audio/transcriptions, so falling back produces 404s. Force
        # the user to configure LLM_AUDIO_* explicitly.
        raise ValueError(
            "audio profile is not configured; set LLM_AUDIO_* "
            "(or guard the call with has_audio_profile())"
        )
    p = resolve_profile(settings, "audio")
    if p.provider not in ("openai", "openai-compatible"):
        raise ValueError(
            "audio profile requires an OpenAI-compatible provider "
            "(Anthropic does not offer audio transcription)"
        )
    return OpenAIAudioClient(p)


def reset_clients_cache() -> None:
    """Test helper: drop cached clients so a settings change takes effect."""
    get_chat_client.cache_clear()
    get_audio_client.cache_clear()
