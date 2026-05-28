"""suggest_lifecycle — unified active→demoted→archived stepper.

DESIGN.md §9.1 + §9.4 + §14.4 #4.

One periodic kind walks BOTH transitions in lockstep:
  active   →  demoted   (via _select_demotion_candidates)
  demoted  →  archived  (via _select_archival_candidates)

Why merged: the two were always called by the same scheduler with adjacent
intervals, share `_apply_decisions`, share the journal-as-activity-signal
filter, and report the same per-entry outcome shape. Two kinds was bookkeeping
without behavioural value.

Payload (all optional):
  {"phases": ["demote", "archive"]}      # default: both, in this order
  {"demote": {"inactive_days":30, "min_age_days":14, "cap":50}}
  {"archive": {"inactive_days":90, "min_demoted_days":30, "cap":50}}

Outcome rows still use phase-specific task_kind values
("suggest_demotion" / "suggest_archival") for backwards-compat with
existing audit/analytic queries that key off task_kind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from marginalia.config import get_settings
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_SUGGEST_LIFECYCLE, task_handler

log = logging.getLogger(__name__)

DEMOTE_INACTIVE_DAYS = 30
DEMOTE_MIN_AGE_DAYS = 14
ARCHIVE_INACTIVE_DAYS = 90
ARCHIVE_MIN_DEMOTED_DAYS = 30
LIFECYCLE_BATCH_CAP = 50

# Outcome task_kind labels — kept distinct so analytics that group by
# task_kind keep working.
PHASE_OUTCOME_KIND = {
    "demote": "suggest_demotion",
    "archive": "suggest_archival",
}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass(slots=True)
class _Decision:
    entry_id: str
    old_lifecycle: str
    new_lifecycle: str
    reason: str

@task_handler(KIND_SUGGEST_LIFECYCLE)
async def handle_suggest_lifecycle(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    phases = list(payload.get("phases") or ["demote", "archive"])
    if not payload.get("force") and not get_settings().auto_lifecycle_enabled:
        await _record_disabled_outcomes(now, phases)
        log.info("suggest_lifecycle skipped: AUTO_LIFECYCLE_ENABLED=false")
        return

    summary: dict[str, Any] = {}
    for phase in phases:
        phase_payload = dict(payload.get(phase) or {})
        if phase == "demote":
            applied, candidates = await _run_demote(now, phase_payload)
        elif phase == "archive":
            applied, candidates = await _run_archive(now, phase_payload)
        else:
            log.warning("suggest_lifecycle: unknown phase %r — skipped", phase)
            continue
        summary[phase] = {"applied": applied, "candidates": candidates}

    log.info("suggest_lifecycle: %s", summary)

async def _record_disabled_outcomes(now: datetime, phases: list[str]) -> None:
    async with session_scope() as session:
        for phase in phases:
            outcome_kind = PHASE_OUTCOME_KIND.get(phase)
            if outcome_kind is None:
                continue
            await record_outcome(
                session,
                task_kind=outcome_kind,
                object_kind=GLOBAL_OBJECT_KIND,
                object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={
                    "disabled": True,
                    "reason": "AUTO_LIFECYCLE_ENABLED=false",
                },
            )
        await session.commit()

async def _run_demote(
    now: datetime, payload: Mapping[str, Any]
) -> tuple[int, int]:
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
    return await _apply_decisions(
        decisions=decisions,
        outcome_task_kind=PHASE_OUTCOME_KIND["demote"],
        now=now,
        summary_extra={
            "inactive_days": inactive_days,
            "min_age_days": min_age_days,
        },
    ), len(decisions)

async def _run_archive(
    now: datetime, payload: Mapping[str, Any]
) -> tuple[int, int]:
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
    return await _apply_decisions(
        decisions=decisions,
        outcome_task_kind=PHASE_OUTCOME_KIND["archive"],
        now=now,
        summary_extra={
            "inactive_days": inactive_days,
            "min_demoted_days": min_demoted_days,
        },
    ), len(decisions)

async def _recent_entry_ids(session, cutoff: datetime) -> set[str]:
    arrays = await journal_repo.list_entry_id_arrays_since(session, cutoff)
    out: set[str] = set()
    for row in arrays:
        for eid in row:
            if isinstance(eid, str):
                out.add(eid)
    return out

async def _select_demotion_candidates(
    *, cutoff_recent_journal: datetime, cutoff_age: datetime, cap: int,
) -> list[_Decision]:
    async with session_scope() as session:
        recent = await _recent_entry_ids(session, cutoff_recent_journal)
        rows = await entries_repo.list_active_for_demotion(
            session, cutoff_age=cutoff_age,
        )
        decisions: list[_Decision] = []
        for entry_id, _created_at in rows:
            if entry_id in recent:
                continue
            decisions.append(_Decision(
                entry_id=entry_id, old_lifecycle="active",
                new_lifecycle="demoted",
                reason=f"no journal mention since {cutoff_recent_journal.isoformat()}",
            ))
            if len(decisions) >= cap:
                break
        await session.commit()
    return decisions

async def _select_archival_candidates(
    *, cutoff_recent_journal: datetime, cutoff_demoted: datetime, cap: int,
) -> list[_Decision]:
    async with session_scope() as session:
        recent = await _recent_entry_ids(session, cutoff_recent_journal)
        rows = await entries_repo.list_demoted_for_archive(
            session, cutoff_demoted=cutoff_demoted,
        )
        decisions: list[_Decision] = []
        for entry_id, _updated_at in rows:
            if entry_id in recent:
                continue
            decisions.append(_Decision(
                entry_id=entry_id, old_lifecycle="demoted",
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
    outcome_task_kind: str,
    now: datetime,
    summary_extra: dict[str, Any],
) -> int:
    applied = 0
    async with session_scope() as session:
        for d in decisions:
            rc = await entries_repo.transition_lifecycle(
                session,
                entry_id=d.entry_id,
                from_lifecycle=d.old_lifecycle,
                to_lifecycle=d.new_lifecycle,
                now=now,
            )
            if not rc:
                await record_outcome(
                    session,
                    task_kind=outcome_task_kind,
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

            await audit_events_repo.append(
                session,
                kind="lifecycle_changed",
                payload={
                    "entry_id": d.entry_id,
                    "old": d.old_lifecycle,
                    "new": d.new_lifecycle,
                    "trigger": outcome_task_kind,
                    "reason": d.reason,
                },
            )
            await record_outcome(
                session,
                task_kind=outcome_task_kind,
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
            task_kind=outcome_task_kind,
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
        log.info("%s: applied=%d / candidates=%d",
                 outcome_task_kind, applied, len(decisions))
    return applied
