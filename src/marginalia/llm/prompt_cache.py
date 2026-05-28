"""Prompt-layout helpers for provider-side prefix caches.

DeepSeek's disk cache only hits a prefix after that prefix has been stored
as a complete cache unit. A stable prelude followed by a changing payload in
one user message therefore may miss on the second request (`A+B` then `A+C`).

These helpers put the stable prelude in its own user message, followed by a
fixed assistant acknowledgement, then append the variable payload as the live
user message. Providers that cache complete message prefixes can then reuse
the stable unit as soon as it has been written. Anthropic callers should keep
`cache_breakpoints=[0]` so the first user message is explicitly cache-marked.
"""
from __future__ import annotations

from collections.abc import Sequence

from marginalia.llm.types import ChatMessage, ContentBlock, TextBlock

CACHE_PREFIX_ACK = (
    "Context received. I will apply it to the next user message."
)


def cacheable_prefix_messages(stable_prefix: str) -> list[ChatMessage]:
    """Return the stable prefix as a complete message pair."""
    return [
        ChatMessage(role="user", content=[TextBlock(text=stable_prefix)]),
        ChatMessage(role="assistant", content=CACHE_PREFIX_ACK),
    ]


def cacheable_prompt_messages(
    stable_prefix: str,
    variable_content: str | Sequence[ContentBlock],
) -> list[ChatMessage]:
    """Build a cache-friendly prompt from stable and variable parts."""
    if isinstance(variable_content, str):
        payload: str | list[ContentBlock] = variable_content
    else:
        payload = list(variable_content)
    return cacheable_prefix_messages(stable_prefix) + [
        ChatMessage(role="user", content=payload),
    ]
