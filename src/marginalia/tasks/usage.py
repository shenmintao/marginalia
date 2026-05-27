"""Per-task LLM usage accumulator.

A ContextVar-backed dict carries token / cache / call counters from inside
handler code back out to TaskRunner._process, which writes them into a
task_outcomes row.

Why ContextVar: handlers don't take a "usage" parameter, and refactoring
every chat client call site to thread one through would touch hundreds of
spots. ContextVar isolates the value per asyncio Task, so concurrent task
runs in the same TaskRunner don't bleed into each other.

Usage from TaskRunner:

    token = bind_accumulator()
    try:
        await handler(payload)
        usage = current_usage()
    finally:
        unbind_accumulator(token)

Usage from a chat client wrapper (already done in factory.record_chat_use):
    record_chat_use(response.usage)
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from marginalia.llm.types import TokenUsage


@dataclass
class UsageCounters:
    """Running totals for one task run."""
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    llm_calls: int = 0
    tool_calls: int = 0

    def add(self, usage: TokenUsage) -> None:
        self.tokens_in += usage.input_tokens or 0
        self.tokens_out += usage.output_tokens or 0
        self.cache_read += usage.cache_read_tokens or 0
        self.cache_creation += usage.cache_creation_tokens or 0
        self.llm_calls += 1

    def to_detail(self, *, duration_ms: int) -> dict[str, int]:
        return {
            "duration_ms": duration_ms,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
        }


_current: ContextVar[UsageCounters | None] = ContextVar(
    "marginalia_task_usage", default=None,
)


def bind_accumulator() -> Token:
    """Install a fresh accumulator in the current context. Returns a Token
    the caller MUST pass back to unbind_accumulator to restore the prior
    value (almost always None, but ContextVar discipline matters)."""
    return _current.set(UsageCounters())


def unbind_accumulator(token: Token) -> UsageCounters | None:
    """Read the current accumulator and detach it. The reset is necessary
    because the asyncio Task may be reused for an unrelated context if the
    TaskRunner is wrapped in a higher-level scheduler."""
    counters = _current.get()
    _current.reset(token)
    return counters


def current_usage() -> UsageCounters | None:
    return _current.get()


def record_chat_use(usage: TokenUsage) -> None:
    """Increment the bound accumulator (no-op when nothing is bound — e.g.
    a chat-page request, which records its own usage on the Conversation
    row instead)."""
    counters = _current.get()
    if counters is None:
        return
    counters.add(usage)


def record_tool_call() -> None:
    counters = _current.get()
    if counters is not None:
        counters.tool_calls += 1
