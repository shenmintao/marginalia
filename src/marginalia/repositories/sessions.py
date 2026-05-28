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

from sqlalchemy import func, select
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


async def list_sessions(
    db: AsyncSession, *, limit: int = 50, offset: int = 0,
) -> list[Session]:
    """Sessions ordered most-recent-first, for the chat sidebar.
    Soft-deleted rows are hidden."""
    rows = (
        await db.execute(
            select(Session)
            .where(Session.deleted_at.is_(None))
            .order_by(Session.started_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return list(rows)


async def get_live(db: AsyncSession, session_id: str) -> Session | None:
    """Load a session row, returning None if it's soft-deleted or missing.
    Use instead of `db.get(Session, id)` on user-facing code paths."""
    s = await db.get(Session, session_id)
    if s is None or s.deleted_at is not None:
        return None
    return s


async def update_session_name(
    db: AsyncSession, session_id: str, name: str,
) -> Session | None:
    """Store the user-facing session title.

    Historically `initiating_user_message` doubled as the sidebar preview.
    The planner now supplies a concise session name, so this field becomes
    the read-side title without a schema migration.
    """
    title = name.strip()
    if not title:
        return None
    s = await get_live(db, session_id)
    if s is None:
        return None
    s.initiating_user_message = title[:160]
    return s


async def soft_delete(db: AsyncSession, session_id: str) -> Session | None:
    """Mark a session deleted_at=now. Returns the row on success, None if
    missing or already deleted. Conversations + journal rows are kept;
    journal is the agent's first-class memory and must survive UI deletes."""
    s = await db.get(Session, session_id)
    if s is None or s.deleted_at is not None:
        return None
    s.deleted_at = _utcnow()
    return s


async def first_user_messages(
    db: AsyncSession, session_ids: list[str],
) -> dict[str, str]:
    """For each session id, the user_message of its lowest-turn_index
    Conversation. Used as a read-side fallback for legacy sessions whose
    `initiating_user_message` was never backfilled.

    Returns a dict keyed by session_id; missing keys mean the session has
    no conversations yet. Single batched query.
    """
    if not session_ids:
        return {}
    sub = (
        select(
            Conversation.session_id,
            func.min(Conversation.turn_index).label("min_turn"),
        )
        .where(Conversation.session_id.in_(session_ids))
        .group_by(Conversation.session_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(Conversation.session_id, Conversation.user_message)
            .join(
                sub,
                (Conversation.session_id == sub.c.session_id)
                & (Conversation.turn_index == sub.c.min_turn),
            )
        )
    ).all()
    return {sid: msg for sid, msg in rows if msg}


async def list_for_session_ordered(
    db: AsyncSession, session_id: str,
) -> list[Conversation]:
    """Conversations in turn_index order — for transcript replay."""
    rows = (
        await db.execute(
            select(Conversation)
            .where(Conversation.session_id == session_id)
            .order_by(Conversation.turn_index.asc())
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
        total_cache_read=0,
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
    conv.total_cache_read = (conv.total_cache_read or 0) + cache_read_tokens
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
    s.total_cache_read = sum(c.total_cache_read or 0 for c in convs)
    s.total_tool_calls = sum(c.total_tool_calls or 0 for c in convs)
    s.total_llm_calls = sum(c.total_llm_calls or 0 for c in convs)
    s.total_duration_ms = sum(c.total_duration_ms or 0 for c in convs)
    s.total_cost_estimate = sum(
        (c.total_cost_estimate or Decimal("0")) for c in convs
    ) or Decimal("0")
    s.ended_at = _utcnow()
    s.end_reason = end_reason
    return s
