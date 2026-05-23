"""suggest_demotion / suggest_archival — design.md §9.1 + §9.4 + §14.4 #4.

Two pure-statistics handlers (no LLM). They walk the lifecycle state machine:

  active → demoted → archived

Triggers (design §9.4 #6 + §14.4 #4):
  - active → demoted (suggest_demotion):
      No `journal` row mentioning this entry_id within DEMOTE_INACTIVE_DAYS
      (default 30d), AND the entry was created at least DEMOTE_MIN_AGE_DAYS
      ago (default 14d, so freshly-uploaded files don't get demoted before
      anyone has had a chance to use them).
  - demoted → archived (suggest_archival):
      Already demoted, no journal mention within ARCHIVE_INACTIVE_DAYS
      (default 90d), AND demoted-state has been in place for at least
      ARCHIVE_MIN_DEMOTED_DAYS (default 30d).

Why journal as the activity signal? Two reasons:
  1. We never read audit_events for business logic (design §14.3).
  2. journal is the canonical "agent touched this entry" record — every
     reflect_turn writes the entry_ids it processed. That's exactly the
     activity we want to count.

Caveat: an entry that was used in a conversation but produced an empty
reflect_turn (no journal rows, just `applied` in task_outcomes) won't be
counted as active. Acceptable: the agent can always promote an entry by
either reading it explicitly or by writing it into a future journal note.
manual_active is the user's escape hatch.

State-machine guarantees (design.md §14.4 #4):
  - manual_active / manual_archived NEVER change automatically
  - no auto-promotion (active ← demoted ← archived)

Cap: at most LIFECYCLE_BATCH_CAP (50) transitions per run, ordered by
"most stale first" (oldest journal mention or earliest created_at).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlalchemy import and_, exists, not_, select, update

from marginalia.db.models import AuditEvent, FileEntry, Journal
from marginalia.db.session import session_scope
from marginalia.services.audit import write_event
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import (
    KIND_SUGGEST_ARCHIVAL,
    KIND_SUGGEST_DEMOTION,
    task_handler,
)

log = logging.getLogger(__name__)

DEMOTE_INACTIVE_DAYS = 30
DEMOTE_MIN_AGE_DAYS = 14
ARCHIVE_INACTIVE_DAYS = 90
ARCHIVE_MIN_DEMOTED_DAYS = 30
LIFECYCLE_BATCH_CAP = 50


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _Decision:
    entry_id: str
    old_lifecycle: str
    new_lifecycle: str
    reason: str


@task_handler(KIND_SUGGEST_DEMOTION)
async def handle_suggest_demotion(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    inactive_days = int(payload.get("inactive_days") or DEMOTE_INACTIVE_DAYS)
    min_age_days = int(payload.get("min_age_days") or DEMOTE_MIN_AGE_DAYS)
    cap = int(payload.get("cap") or LIFECYCLE_BATCH_CAP)

    cutoff_recent_journal = now - timedelta(days=inactive_days)
    cutoff_age = now - timedelta(days=min_age_days)

    decisions = await _select_demotion_candidates(
        cutoff_recent_journal=cutoff_recent_journal,
        cutoff_age=cutoff_age,
        cap=cap,
    )
    await _apply_decisions(
        decisions=decisions,
        task_kind=KIND_SUGGEST_DEMOTION,
        now=now,
        summary_extra={
            "inactive_days": inactive_days,
            "min_age_days": min_age_days,
        },
    )


@task_handler(KIND_SUGGEST_ARCHIVAL)
async def handle_suggest_archival(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    inactive_days = int(payload.get("inactive_days") or ARCHIVE_INACTIVE_DAYS)
    min_demoted_days = int(payload.get("min_demoted_days") or ARCHIVE_MIN_DEMOTED_DAYS)
    cap = int(payload.get("cap") or LIFECYCLE_BATCH_CAP)

    cutoff_recent_journal = now - timedelta(days=inactive_days)
    cutoff_demoted = now - timedelta(days=min_demoted_days)

    decisions = await _select_archival_candidates(
        cutoff_recent_journal=cutoff_recent_journal,
        cutoff_demoted=cutoff_demoted,
        cap=cap,
    )
    await _apply_decisions(
        decisions=decisions,
        task_kind=KIND_SUGGEST_ARCHIVAL,
        now=now,
        summary_extra={
            "inactive_days": inactive_days,
            "min_demoted_days": min_demoted_days,
        },
    )


async def _select_demotion_candidates(
    *,
    cutoff_recent_journal: datetime,
    cutoff_age: datetime,
    cap: int,
) -> list[_Decision]:
    async with session_scope() as session:
        # Subquery: entry_ids that appear in any journal row written since
        # cutoff_recent_journal. SQLite doesn't support ANY() over JSON arrays
        # cleanly, so we do this server-side: pull recent journal rows,
        # flatten entry_ids, then filter the SELECT in Python.
        recent_rows = (
            await session.execute(
                select(Journal.entry_ids).where(Journal.created_at >= cutoff_recent_journal)
            )
        ).scalars().all()
        recent_entry_ids: set[str] = set()
        for row in recent_rows:
            for eid in (row or []):
                if isinstance(eid, str):
                    recent_entry_ids.add(eid)

        # Candidate filter: lifecycle='active' (NOT manual_active),
        # created_at <= cutoff_age, deleted_at IS NULL.
        rows = (
            await session.execute(
                select(FileEntry.id, FileEntry.created_at)
                .where(
                    FileEntry.lifecycle == "active",
                    FileEntry.deleted_at.is_(None),
                    FileEntry.created_at <= cutoff_age,
                )
                .order_by(FileEntry.created_at.asc())
            )
        ).all()

        decisions: list[_Decision] = []
        for entry_id, created_at in rows:
            if entry_id in recent_entry_ids:
                continue
            decisions.append(_Decision(
                entry_id=entry_id,
                old_lifecycle="active",
                new_lifecycle="demoted",
                reason=f"no journal mention since {cutoff_recent_journal.isoformat()}",
            ))
            if len(decisions) >= cap:
                break
        await session.commit()
    return decisions


async def _select_archival_candidates(
    *,
    cutoff_recent_journal: datetime,
    cutoff_demoted: datetime,
    cap: int,
) -> list[_Decision]:
    async with session_scope() as session:
        recent_rows = (
            await session.execute(
                select(Journal.entry_ids).where(Journal.created_at >= cutoff_recent_journal)
            )
        ).scalars().all()
        recent_entry_ids: set[str] = set()
        for row in recent_rows:
            for eid in (row or []):
                if isinstance(eid, str):
                    recent_entry_ids.add(eid)

        # We approximate "demoted for >= min_demoted_days" by looking at the
        # latest audit_events row of kind='lifecycle_changed' that set this
        # entry to 'demoted'. Wait — design §14.3 forbids reading audit for
        # business logic. So instead, use FileEntry.updated_at as the proxy:
        # a row currently in 'demoted' state has updated_at = the time of
        # last lifecycle change (or any other field change since). If
        # updated_at is older than cutoff_demoted, the entry has been "stably
        # demoted" for at least that long.
        #
        # Edge case: any other UPDATE on the row resets updated_at, which
        # may delay archival. Acceptable: archival is conservative on
        # purpose, and any user / AI activity touching the entry is precisely
        # the kind of signal we don't want to override.
        rows = (
            await session.execute(
                select(FileEntry.id, FileEntry.updated_at)
                .where(
                    FileEntry.lifecycle == "demoted",
                    FileEntry.deleted_at.is_(None),
                    FileEntry.updated_at <= cutoff_demoted,
                )
                .order_by(FileEntry.updated_at.asc())
            )
        ).all()

        decisions: list[_Decision] = []
        for entry_id, _updated_at in rows:
            if entry_id in recent_entry_ids:
                continue
            decisions.append(_Decision(
                entry_id=entry_id,
                old_lifecycle="demoted",
                new_lifecycle="archived",
                reason=f"no journal mention since {cutoff_recent_journal.isoformat()}",
            ))
            if len(decisions) >= cap:
                break
        await session.commit()
    return decisions


async def _apply_decisions(
    *,
    decisions: list[_Decision],
    task_kind: str,
    now: datetime,
    summary_extra: dict[str, Any],
) -> None:
    applied = 0
    async with session_scope() as session:
        for d in decisions:
            result = await session.execute(
                update(FileEntry)
                .where(
                    FileEntry.id == d.entry_id,
                    FileEntry.lifecycle == d.old_lifecycle,
                    FileEntry.deleted_at.is_(None),
                )
                .values(lifecycle=d.new_lifecycle, updated_at=now)
            )
            if not result.rowcount:
                # Lost a race (entry was just touched, deleted, or already
                # transitioned). Record as deferred for visibility.
                await record_outcome(
                    session,
                    task_kind=task_kind,
                    object_kind="file_entry",
                    object_id=d.entry_id,
                    outcome="deferred",
                    detail={
                        "old_lifecycle": d.old_lifecycle,
                        "new_lifecycle": d.new_lifecycle,
                        "reason": "row state changed before update",
                    },
                )
                continue

            await write_event(
                session,
                kind="lifecycle_changed",
                payload={
                    "entry_id": d.entry_id,
                    "old": d.old_lifecycle,
                    "new": d.new_lifecycle,
                    "trigger": task_kind,
                    "reason": d.reason,
                },
            )
            await record_outcome(
                session,
                task_kind=task_kind,
                object_kind="file_entry",
                object_id=d.entry_id,
                outcome="applied",
                detail={
                    "old_lifecycle": d.old_lifecycle,
                    "new_lifecycle": d.new_lifecycle,
                    "reason": d.reason,
                },
            )
            applied += 1

        await record_outcome(
            session,
            task_kind=task_kind,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if applied else "noop",
            detail={
                "candidates": len(decisions),
                "applied": applied,
                **summary_extra,
            },
        )
        await session.commit()

    if applied:
        log.info("%s: applied=%d / candidates=%d", task_kind, applied, len(decisions))
