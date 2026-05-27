"""periodic_tick — the dispatcher (DESIGN.md §9.1 + §9.3).

This is the lowest-priority task in the system (priority 300). Its job each
firing:
  1. Walk PERIODIC_INTERVALS. For each (kind, interval):
     - if a pending/running row already exists for kind k, skip
     - otherwise look up the most recent done row's finished_at; if (now -
       finished_at) >= interval, enqueue(kind=k, dedup_key=k)
  2. Dispatch per-session work that doesn't fit the global-kind pattern:
     for each session with ≥MIN_TURNS reflect_turn rows and no recent
     summarize outcome, enqueue summarize_session(session_id=sid).
  3. Re-enqueue self (kind='periodic_tick') 10 minutes from now, with
     dedup_key='periodic_tick' to keep at most one in flight.

`recover_stuck_tasks` / `prune` are dispatched through here — they appear
in PERIODIC_INTERVALS. The tick itself is NOT listed there; it self-schedules
so the chain never breaks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.repositories import files as files_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories import task_outcomes as task_outcomes_repo
from marginalia.repositories import tasks as tasks_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.services.reprocess import reprocess_file
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import (
    KIND_PERIODIC_TICK,
    KIND_SUMMARIZE_SESSION,
    PERIODIC_INTERVALS,
    task_handler,
)

log = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 600  # 10 minutes
SUMMARIZE_MIN_TURNS = 3
SUMMARIZE_MIN_AGE = timedelta(hours=24)
SUMMARIZE_MAX_DISPATCH_PER_TICK = 10

# Self-heal: files whose ingest finished but produced a useless summary.
# Trim threshold matches kb-lite's 50-char rule of thumb — anything
# shorter is essentially "we tried, the LLM gave up". Cooldown matches
# the summarize cadence so a stuck file doesn't churn every tick.
LOW_QUALITY_MIN_SUMMARY_CHARS = 50
LOW_QUALITY_COOLDOWN = timedelta(hours=24)
LOW_QUALITY_MAX_DISPATCH_PER_TICK = 5
LOW_QUALITY_OUTCOME_KIND = "reprocess_low_quality"

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; coerce to UTC-aware for arithmetic."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

@task_handler(KIND_PERIODIC_TICK)
async def handle_periodic_tick(payload: Mapping[str, Any]) -> None:
    now = _utcnow()

    async with session_scope() as session:
        dispatched: list[str] = []
        skipped_recent: list[str] = []
        skipped_inflight: list[str] = []

        for kind, interval in PERIODIC_INTERVALS.items():
            if await tasks_repo.has_inflight_for_kind(session, kind):
                skipped_inflight.append(kind)
                continue

            last_done_at = _aware(
                await tasks_repo.last_done_at_for_kind(session, kind)
            )

            if last_done_at is not None and (now - last_done_at) < interval:
                skipped_recent.append(kind)
                continue

            task = await enqueue(
                session,
                kind=kind,
                payload={},
                dedup_key=kind,
            )
            if task is not None:
                dispatched.append(kind)
                await audit_events_repo.append(
                    session,
                    kind="task_enqueued",
                    task_id=task.id,
                    payload={"kind": kind, "scheduled_by": "periodic_tick"},
                )

        # Per-session summarize dispatch (doesn't fit the global PERIODIC_INTERVALS
        # pattern — one task per eligible session, dedup_key encodes session_id).
        summarize_dispatched = await _dispatch_summarize_sessions(session, now)
        if summarize_dispatched:
            dispatched.append(
                f"{KIND_SUMMARIZE_SESSION}({len(summarize_dispatched)})"
            )

        # Self-heal: re-ingest files whose summary came out empty/short.
        # Same shape as summarize dispatch — per-file fanout with
        # task_outcomes cooldown so a stubbornly bad file doesn't churn.
        low_q_dispatched = await _dispatch_reprocess_low_quality(session, now)
        if low_q_dispatched:
            dispatched.append(
                f"{LOW_QUALITY_OUTCOME_KIND}({len(low_q_dispatched)})"
            )

        next_run = now + timedelta(seconds=TICK_INTERVAL_SECONDS)
        await enqueue(
            session,
            kind=KIND_PERIODIC_TICK,
            payload={},
            dedup_key=KIND_PERIODIC_TICK,
            scheduled_at=next_run,
        )

        await record_outcome(
            session,
            task_kind=KIND_PERIODIC_TICK,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if dispatched else "noop",
            detail={
                "dispatched": dispatched,
                "skipped_recent": skipped_recent,
                "skipped_inflight": skipped_inflight,
                "next_tick_at": next_run.isoformat(),
            },
        )
        await session.commit()

async def _dispatch_summarize_sessions(session, now: datetime) -> list[str]:
    """Find sessions that have accumulated enough reflect_turn rows and
    haven't been summarized recently; enqueue a summarize_session task
    per session, capped at SUMMARIZE_MAX_DISPATCH_PER_TICK.

    Eligibility:
      - The session has ≥ SUMMARIZE_MIN_TURNS reflect_turn journal rows
        (any turns count, not necessarily consecutive).
      - The most-recent reflect_turn row is older than SUMMARIZE_MIN_AGE
        (gives an in-flight session room to accumulate before we touch it).
      - No `summarize_session` task_outcomes row for this session within
        SUMMARIZE_MIN_AGE (handler also re-checks; this is just early
        filtering to avoid noisy enqueues).
    """
    age_cutoff = now - SUMMARIZE_MIN_AGE
    rows = await journal_repo.reflect_per_session_with_max(
        session,
        min_count=SUMMARIZE_MIN_TURNS,
        max_newest=age_cutoff,
        limit=SUMMARIZE_MAX_DISPATCH_PER_TICK * 4,
    )

    enqueued: list[str] = []
    for sid, _count, _newest in rows:
        if len(enqueued) >= SUMMARIZE_MAX_DISPATCH_PER_TICK:
            break

        last_outcome = _aware(
            await task_outcomes_repo.latest_completed_at_for(
                session,
                task_kind=KIND_SUMMARIZE_SESSION,
                object_kind="session",
                object_id=sid,
            )
        )
        if last_outcome is not None and (now - last_outcome) < SUMMARIZE_MIN_AGE:
            continue

        task = await enqueue(
            session,
            kind=KIND_SUMMARIZE_SESSION,
            payload={"session_id": sid},
            dedup_key=f"{KIND_SUMMARIZE_SESSION}:{sid}",
        )
        if task is not None:
            enqueued.append(sid)
            await audit_events_repo.append(
                session,
                kind="task_enqueued",
                task_id=task.id,
                payload={
                    "kind": KIND_SUMMARIZE_SESSION,
                    "session_id": sid,
                    "scheduled_by": "periodic_tick",
                },
            )
    return enqueued

async def _dispatch_reprocess_low_quality(session, now: datetime) -> list[str]:
    """Find ingested files with empty or short summaries and re-enqueue
    ingest_file for each, capped at LOW_QUALITY_MAX_DISPATCH_PER_TICK.

    Eligibility:
      - File is live and already ingested (`ingested_at IS NOT NULL`).
      - Summary is NULL or trims to fewer than LOW_QUALITY_MIN_SUMMARY_CHARS.
      - No prior `reprocess_low_quality` outcome for this file within
        LOW_QUALITY_COOLDOWN (otherwise we'd churn the same broken file
        every 10 minutes — the LLM hasn't gotten smarter in 600s).

    Cooldown is recorded as a `task_outcomes` row keyed on
    (LOW_QUALITY_OUTCOME_KIND, "file", file_id). We record `applied`
    when we actually enqueue and `noop` when dedup short-circuits — both
    count as "we tried", so the cooldown applies either way.
    """
    candidates = await files_repo.find_low_quality(
        session,
        min_summary_chars=LOW_QUALITY_MIN_SUMMARY_CHARS,
        limit=LOW_QUALITY_MAX_DISPATCH_PER_TICK * 4,
    )

    enqueued: list[str] = []
    for fid in candidates:
        if len(enqueued) >= LOW_QUALITY_MAX_DISPATCH_PER_TICK:
            break

        last_outcome = _aware(
            await task_outcomes_repo.latest_completed_at_for(
                session,
                task_kind=LOW_QUALITY_OUTCOME_KIND,
                object_kind="file",
                object_id=fid,
            )
        )
        if last_outcome is not None and (now - last_outcome) < LOW_QUALITY_COOLDOWN:
            continue

        from marginalia.db.models import File  # local — avoid widening top imports
        file_row = await session.get(File, fid)
        if file_row is None or file_row.deleted_at is not None:
            continue

        task_id = await reprocess_file(
            session, file_row, scheduled_by="periodic_tick:low_quality",
        )
        await record_outcome(
            session,
            task_kind=LOW_QUALITY_OUTCOME_KIND,
            object_kind="file",
            object_id=fid,
            outcome="applied" if task_id else "noop",
            detail={
                "file_id": fid,
                "task_id": task_id,
                "summary_len": len((file_row.summary or "").strip()),
            },
        )
        if task_id is not None:
            enqueued.append(fid)
    return enqueued


async def bootstrap_periodic_tick() -> None:
    """Ensure exactly one periodic_tick row exists at runner startup.

    Idempotent: if a pending/running tick already exists, no-op. Otherwise
    enqueue one due immediately so the dispatcher kicks in on the next claim.

    Skip entirely when no LLM api_key is configured. Every downstream
    fan-out (tag_quality, normalize_tags, summarize_session) hits the LLM
    on its first step, so without a key the worker crashes in a loop with
    OpenAIError: Missing credentials. Bootstrap re-runs on next startup,
    so the user just sets a key in Settings and restarts.
    """
    from marginalia.config import LlmConfigError, get_settings, validate_llm_config
    try:
        validate_llm_config(get_settings())
    except LlmConfigError as e:
        log.warning("bootstrap_periodic_tick skipped: %s", e)
        return

    async with session_scope() as session:
        if await tasks_repo.has_inflight_for_kind(session, KIND_PERIODIC_TICK):
            await session.commit()
            return
        await enqueue(
            session,
            kind=KIND_PERIODIC_TICK,
            payload={"reason": "bootstrap"},
            dedup_key=KIND_PERIODIC_TICK,
        )
        await session.commit()
