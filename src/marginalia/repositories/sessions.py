"""sessions / conversations service — DESIGN.md §8.2 + §10.2 + §12.2.

Wraps reads/writes against the audit-layer container tables (sessions and
conversations). Keeps the runtime free of bookkeeping noise.

Conventions:
  - Sessions are created lazily by `create_session()`. Their `total_*`
    counters are recomputed by `close_session()` from constituent
    conversations.
  - Each turn is a conversation. `start_conversation()` inserts the row at
    user_message; `append_llm_call()` / `append_tool_call()` mutate the JSON
    arrays + total_* counters in real time; `finalize_conversation()` writes
    agent_response + ended_at and rolls totals into the parent session.
  - JSON arrays are appended to via SQLAlchemy attribute mutation (the rows
    are reloaded fresh inside session_scope, mutated, and committed). For
    SQLite this works because we re-assign the list; on Postgres jsonb the
    same idiom is fine.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Conversation, Session
from marginalia.utils.ids import new_id


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_session(
    db: AsyncSession,
    *,
    initiating_user_message: str,
) -> Session:
    now = _utcnow()
    s = Session(
        id=new_id(),
        started_at=now,
        ended_at=None,
        end_reason=None,
        initiating_user_message=initiating_user_message,
        turn_count=0,
        total_input_tokens=0,
        total_output_tokens=0,
        total_cache_read=0,
        total_tool_calls=0,
        total_llm_calls=0,
        total_cost_estimate=Decimal("0"),
        total_duration_ms=0,
    )
    db.add(s)
    await db.flush()
    return s


async def latest_turn_index(
    db: AsyncSession, session_id: str,
) -> int | None:
    """Highest turn_index already recorded for a session, or None if there
    are no conversations yet. Used by the runtime to compute the next turn."""
    return (
        await db.execute(
            select(Conversation.turn_index)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.turn_index.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def list_conversation_ids(
    db: AsyncSession, session_id: str,
) -> list[str]:
    """All conversation ids belonging to `session_id`, unordered."""
    rows = (
        await db.execute(
            select(Conversation.id).where(Conversation.session_id == session_id)
        )
    ).scalars().all()
    return list(rows)


async def last_conversation_id(
    db: AsyncSession, session_id: str,
) -> str | None:
    """Conversation id with the highest turn_index in `session_id`."""
    return (
        await db.execute(
            select(Conversation.id)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.turn_index.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_conversation(
    db: AsyncSession, conversation_id: str,
) -> Conversation | None:
    """Load a conversation row by id (None if missing)."""
    return await db.get(Conversation, conversation_id)


async def list_for_session(
    db: AsyncSession, session_id: str,
) -> list[Conversation]:
    """All conversation rows for `session_id`. Used by close_session
    to roll up totals."""
    rows = (
        await db.execute(
            select(Conversation).where(Conversation.session_id == session_id)
        )
    ).scalars().all()
    return list(rows)


async def start_conversation(
    db: AsyncSession,
    *,
    session_id: str,
    turn_index: int,
    user_message: str,
) -> Conversation:
    now = _utcnow()
    c = Conversation(
        id=new_id(),
        session_id=session_id,
        turn_index=turn_index,
        started_at=now,
        ended_at=None,
        user_message=user_message,
        agent_response=None,
        tool_calls=[],
        llm_calls=[],
        total_input_tokens=0,
        total_output_tokens=0,
        total_tool_calls=0,
        total_llm_calls=0,
        total_duration_ms=0,
        total_cost_estimate=Decimal("0"),
    )
    db.add(c)
    await db.flush()
    return c


async def append_llm_call(
    db: AsyncSession,
    *,
    conversation_id: str,
    phase: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    duration_ms: int = 0,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Append one LLM call record to conversation.llm_calls and bump totals.

    `phase` ∈ {'plan','execute'}. `extra` may carry citations, plan text, etc.
    """
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} missing")
    record = {
        "phase": phase,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "duration_ms": duration_ms,
        "at": _utcnow().isoformat(),
    }
    if extra:
        record.update(dict(extra))
    conv.llm_calls = list(conv.llm_calls or []) + [record]
    conv.total_llm_calls = (conv.total_llm_calls or 0) + 1
    conv.total_input_tokens = (conv.total_input_tokens or 0) + input_tokens
    conv.total_output_tokens = (conv.total_output_tokens or 0) + output_tokens
    conv.total_duration_ms = (conv.total_duration_ms or 0) + duration_ms


async def append_tool_call(
    db: AsyncSession,
    *,
    conversation_id: str,
    name: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any] | None,
    error: str | None = None,
    duration_ms: int = 0,
) -> None:
    """Append one tool call record to conversation.tool_calls and bump totals."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} missing")
    record = {
        "name": name,
        "arguments": dict(arguments),
        "result": dict(result) if result is not None else None,
        "error": error,
        "duration_ms": duration_ms,
        "at": _utcnow().isoformat(),
    }
    conv.tool_calls = list(conv.tool_calls or []) + [record]
    conv.total_tool_calls = (conv.total_tool_calls or 0) + 1
    conv.total_duration_ms = (conv.total_duration_ms or 0) + duration_ms


async def finalize_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    agent_response: str,
) -> Conversation:
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ValueError(f"conversation {conversation_id} missing")
    conv.agent_response = agent_response
    conv.ended_at = _utcnow()
    return conv


async def close_session(
    db: AsyncSession,
    *,
    session_id: str,
    end_reason: str = "normal",
) -> Session:
    """Roll up totals from conversations into the session and stamp ended_at."""
    s = await db.get(Session, session_id)
    if s is None:
        raise ValueError(f"session {session_id} missing")
    convs = await list_for_session(db, session_id)
    s.turn_count = len(convs)
    s.total_input_tokens = sum(c.total_input_tokens or 0 for c in convs)
    s.total_output_tokens = sum(c.total_output_tokens or 0 for c in convs)
    s.total_tool_calls = sum(c.total_tool_calls or 0 for c in convs)
    s.total_llm_calls = sum(c.total_llm_calls or 0 for c in convs)
    s.total_duration_ms = sum(c.total_duration_ms or 0 for c in convs)
    s.total_cost_estimate = sum(
        (c.total_cost_estimate or Decimal("0")) for c in convs
    ) or Decimal("0")
    s.ended_at = _utcnow()
    s.end_reason = end_reason
    return s
