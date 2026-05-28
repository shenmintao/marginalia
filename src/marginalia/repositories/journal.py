"""journal repository — pure SA queries against the Journal table.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Conversation, Journal


async def search(
    db: AsyncSession,
    *,
    cutoff: datetime,
    kinds: Sequence[str],
    conversation_id: str | None,
    include_superseded: bool,
    text: str | Sequence[str] | None,
    order: str,
    limit: int,
    offset: int = 0,
) -> list[Journal]:
    """The shape used by search_journal. JSON-array filters (entry_id, tags)
    are evaluated in Python by the caller — SQLite can't do them cleanly.
    `offset` cooperates with the agent tool's loop-and-page strategy."""
    stmt = select(Journal).where(
        Journal.created_at >= cutoff,
        Journal.source_kind.in_(list(kinds)),
    )
    if conversation_id:
        stmt = stmt.where(Journal.conversation_id == conversation_id)
    if not include_superseded:
        stmt = stmt.where(Journal.superseded_by_id.is_(None))
    text_terms = _text_terms(text)
    if text_terms:
        stmt = stmt.where(or_(
            *(Journal.note.ilike(f"%{term}%") for term in text_terms)
        ))
    if order == "oldest_first":
        stmt = stmt.order_by(Journal.created_at.asc())
    else:
        stmt = stmt.order_by(Journal.created_at.desc())
    if offset:
        stmt = stmt.offset(offset)
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return list(rows)


def _text_terms(text: str | Sequence[str] | None) -> list[str]:
    if text is None:
        return []
    if isinstance(text, str):
        item = text.strip()
        return [item] if item else []
    return [str(item).strip() for item in text if str(item).strip()]


async def recent_journal_for_snapshot(
    db: AsyncSession, *, before: datetime, limit: int,
) -> list[Journal]:
    """Most recent live journal rows whose `created_at < before`, both tiers
    (insight + reflect_turn) mixed. Used by the agent's stable-context
    snapshot.

    Passing the session's `started_at` as `before` lets the snapshot stay
    logically frozen for the duration of the session: rows the session
    itself produces (always written after `started_at`) are naturally
    excluded, with no JOIN to sessions/conversations.
    """
    rows = (
        await db.execute(
            select(Journal)
            .where(
                Journal.superseded_by_id.is_(None),
                Journal.created_at < before,
            )
            .order_by(Journal.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def list_reflect_rows_for_session(
    db: AsyncSession, session_id: str, *, limit: int,
) -> list[tuple[str, str, list, list, datetime, str]]:
    """`(id, note, entry_ids, tags, created_at, conversation_id)` for every
    reflect_turn journal row in `session_id`, oldest first. Used by
    summarize_session."""
    rows = (
        await db.execute(
            select(Journal.id, Journal.note, Journal.entry_ids, Journal.tags,
                   Journal.created_at, Journal.conversation_id)
            .join(Conversation, Conversation.id == Journal.conversation_id)
            .where(
                Conversation.session_id == session_id,
                Journal.source_kind == "reflect_turn",
            )
            .order_by(Journal.created_at.asc())
            .limit(limit)
        )
    ).all()
    return [(jid, n, e, t, c, cid) for jid, n, e, t, c, cid in rows]


async def list_entry_id_lists_for_conversations(
    db: AsyncSession, conversation_ids: list[str],
) -> list[list[str]]:
    """The Journal.entry_ids JSON arrays for every row whose conversation is
    in `conversation_ids`. Caller flattens. Used by summarize_session."""
    if not conversation_ids:
        return []
    rows = (
        await db.execute(
            select(Journal.entry_ids)
            .where(Journal.conversation_id.in_(conversation_ids))
        )
    ).scalars().all()
    return [list(r or []) for r in rows]


async def list_active_insights_recent(
    db: AsyncSession, *, limit: int,
) -> list[tuple[str, str, list, list, datetime]]:
    """`(id, note, entry_ids, tags, created_at)` for the N most recent
    non-superseded insight rows. Used by summarize_session to surface the
    chain the LLM might be replacing."""
    rows = (
        await db.execute(
            select(Journal.id, Journal.note, Journal.entry_ids,
                   Journal.tags, Journal.created_at)
            .where(
                Journal.source_kind == "insight",
                Journal.superseded_by_id.is_(None),
            )
            .order_by(Journal.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [(jid, n, e, t, c) for jid, n, e, t, c in rows]


async def filter_active_insight_ids(
    db: AsyncSession, candidate_ids: list[str],
) -> list[str]:
    """Of `candidate_ids`, keep those whose Journal row is an active
    (non-superseded) insight. Used to validate `superseded` proposals."""
    if not candidate_ids:
        return []
    rows = (
        await db.execute(
            select(Journal.id).where(
                Journal.id.in_(candidate_ids),
                Journal.source_kind == "insight",
                Journal.superseded_by_id.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def mark_superseded(
    db: AsyncSession, ids: list[str], *, by_id: str,
) -> None:
    """Bulk-set `superseded_by_id = by_id` for the given journal ids."""
    if not ids:
        return
    await db.execute(
        update(Journal)
        .where(Journal.id.in_(ids))
        .values(superseded_by_id=by_id)
    )


async def reflect_per_session_with_max(
    db: AsyncSession,
    *,
    min_count: int,
    max_newest: datetime,
    limit: int,
) -> list[tuple[str, int, datetime]]:
    """Return `(session_id, count, max_created_at)` for sessions whose
    reflect_turn rows total at least `min_count` and whose most-recent
    reflect_turn happened at or before `max_newest`. Used by periodic_tick
    to decide which sessions are due for summarization."""
    rows = (
        await db.execute(
            select(
                Conversation.session_id,
                func.count(Journal.id),
                func.max(Journal.created_at),
            )
            .join(Journal, Journal.conversation_id == Conversation.id)
            .where(Journal.source_kind == "reflect_turn")
            .group_by(Conversation.session_id)
            .having(func.count(Journal.id) >= min_count)
            .having(func.max(Journal.created_at) <= max_newest)
            .limit(limit)
        )
    ).all()
    return [(sid, c, mx) for sid, c, mx in rows]


async def list_recent_with_hints(
    db: AsyncSession, *, cutoff: datetime, limit: int,
) -> list[tuple[str | None, list, list, datetime]]:
    """Return `(note, entry_ids, tags, created_at)` for the N most recent
    journal rows newer than `cutoff`. Used by restructure_catalogs to surface
    recent reflect notes carrying `hint:*` tags."""
    rows = (
        await db.execute(
            select(Journal.note, Journal.entry_ids, Journal.tags,
                   Journal.created_at)
            .where(Journal.created_at >= cutoff)
            .order_by(Journal.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [(n, list(e or []), list(t or []), ca) for n, e, t, ca in rows]


async def list_id_entry_ids_note_created(
    db: AsyncSession, *, cutoff: datetime,
) -> list[tuple[str, list, str | None, datetime]]:
    """`(id, entry_ids, note, created_at)` newest-first for journal rows newer
    than `cutoff`. Used by refresh_entry_extra to bucket per-entry mentions."""
    rows = (
        await db.execute(
            select(Journal.id, Journal.entry_ids, Journal.note,
                   Journal.created_at)
            .where(Journal.created_at >= cutoff)
            .order_by(Journal.created_at.desc())
        )
    ).all()
    return [(jid, list(e or []), n, ca) for jid, e, n, ca in rows]


async def list_entry_id_arrays_since(
    db: AsyncSession, cutoff: datetime,
) -> list[list]:
    """All `entry_ids` arrays from journal rows newer than `cutoff`. Used by
    suggest_lifecycle to compute "entries mentioned recently" without
    pulling other columns, and by mine_session_cooccurrence."""
    rows = (
        await db.execute(
            select(Journal.entry_ids).where(Journal.created_at >= cutoff)
        )
    ).scalars().all()
    return [list(r or []) for r in rows]
