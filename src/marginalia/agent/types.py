"""agent runtime types (the bigger picture lives in agent.runtime)."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(slots=True)
class TurnUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    duration_ms: int = 0
    cost_estimate: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass(slots=True)
class TurnResult:
    session_id: str
    conversation_id: str
    agent_response: str
    plan_text: str
    usage: TurnUsage
    truncated: bool = False  # True when MAX_EXECUTE_TURNS hit


@dataclass(slots=True)
class AgentEvent:
    """One frame in the SSE stream produced by `chat()`.

    event_type values:
      - "session"      : data = session_id (sent on first event when session was implicitly created)
      - "conversation" : data = conversation_id (sent right after conversation row opens)
      - "planning"     : transient marker; planner LLM call started, no data
      - "plan"         : data = plan_text (full plan after planner returns)
      - "thinking"     : transient marker; execute LLM call started, no data
      - "tool_call"    : data = JSON{name, arguments, display}
      - "tool_result"  : data = JSON{name, ok, error?, preview?}
      - "user_artifact": data = JSON{tool, payload} - side-channel content
                          (e.g. chart spec) that's shown to the user but
                          intentionally NOT fed back to the model
      - "answer"       : data = final answer text (single chunk; no token-level streaming yet)
      - "error"        : data = error message
      - "done"         : data = JSON usage dict (tokens, tool_calls, llm_calls, duration_ms, truncated, session_name?)
    """

    event_type: str
    data: str = ""


class AgentTurnError(Exception):
    """Raised when a turn cannot be completed (e.g. exceeded loop cap)."""
