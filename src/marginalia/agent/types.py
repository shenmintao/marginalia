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


class AgentTurnError(Exception):
    """Raised when a turn cannot be completed (e.g. exceeded loop cap)."""
