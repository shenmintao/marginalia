"""End-to-end dispatcher / self-healing sanity check.

Run:
    .venv/Scripts/python tests/test_dispatcher_e2e.py

Verifies:
  1. Runner.start() bootstraps a periodic_tick row.
  2. periodic_tick fires → enqueues every kind in PERIODIC_INTERVALS that
     has no recent done row.
  3. dedup_key=kind prevents duplicate scheduling — a second tick within the
     interval window does NOT re-enqueue.
  4. recover_stuck_tasks promotes a row stuck at running+expired-lease back
     to pending (or marks it dead at attempts>=max).
  5. prune (unified handler) deletes audit rows older than the retention window.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_dispatcher_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = "0.1"
os.environ["WORKER_LEASE_SECONDS"] = "5"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import AuditEvent, Base
from marginalia.db.models.tasks import Task
from marginalia.tasks.handlers.periodic_tick import (
    bootstrap_periodic_tick,
    handle_periodic_tick,
    KIND_PERIODIC_TICK,
)
from marginalia.tasks.handlers.prune import handle_prune
from marginalia.tasks.handlers.recover_stuck_tasks import handle_recover_stuck_tasks
from marginalia.tasks.kinds import (
    KIND_PRUNE,
    KIND_RECOVER_STUCK_TASKS,
    PERIODIC_INTERVALS,
)
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def main():
    await _create_schema()
    factory = get_session_factory()

    # --- 1. bootstrap creates exactly one periodic_tick row ----------------
    await bootstrap_periodic_tick()
    async with factory() as s:
        ticks = (await s.execute(
            select(Task).where(Task.kind == KIND_PERIODIC_TICK)
        )).scalars().all()
        assert len(ticks) == 1
        assert ticks[0].dedup_key == KIND_PERIODIC_TICK
        bootstrap_id = ticks[0].id
    print("[1] bootstrap created 1 tick row:", bootstrap_id)

    # bootstrap a second time — should be a no-op (dedup)
    await bootstrap_periodic_tick()
    async with factory() as s:
        n_ticks = (await s.execute(
            text("SELECT COUNT(*) FROM tasks WHERE kind=:k"),
            {"k": KIND_PERIODIC_TICK},
        )).scalar()
        assert n_ticks == 1, f"bootstrap not idempotent: {n_ticks}"
    print("[1] bootstrap idempotent: still 1 tick")

    # --- 2. tick handler dispatches all due periodic kinds -----------------
    await handle_periodic_tick({})
    async with factory() as s:
        rows = (await s.execute(
            select(Task.kind, Task.dedup_key, Task.status)
            .where(Task.kind != KIND_PERIODIC_TICK)
        )).all()
        kinds_dispatched = {k for k, _, _ in rows}
    print("[2] dispatched kinds:", sorted(kinds_dispatched))
    expected = set(PERIODIC_INTERVALS.keys())
    assert kinds_dispatched == expected, f"missing: {expected - kinds_dispatched}, extra: {kinds_dispatched - expected}"

    # tick row scheduling: one row should be the just-completed bootstrap (mark
    # it done so we can verify the next behavior); a fresh tick row should be
    # scheduled in the future
    async with factory() as s:
        ticks = (await s.execute(
            select(Task).where(Task.kind == KIND_PERIODIC_TICK)
            .order_by(Task.created_at)
        )).scalars().all()
    print("[2] tick rows after run:", [(t.id, t.status, t.scheduled_at) for t in ticks])
    # 1 bootstrap (still pending — handler doesn't mark itself done) + 1 future
    pending_ticks = [t for t in ticks if t.status == "pending"]
    # The handler enqueued a future tick with dedup_key=periodic_tick. Because
    # the bootstrap tick is also pending with the same dedup_key, enqueue()
    # actually returned the existing one and did NOT insert a new row.
    assert len(pending_ticks) == 1, f"unexpected pending tick count: {len(pending_ticks)}"

    # --- 3. running tick a 2nd time within interval should NOT re-dispatch -
    # Mark all dispatched periodic-kind rows as done so freshness check kicks in.
    async with factory() as s:
        await s.execute(text(
            "UPDATE tasks SET status='done', finished_at=:n WHERE kind != :tk AND status='pending'"
        ), {"n": _now(), "tk": KIND_PERIODIC_TICK})
        await s.commit()

    await handle_periodic_tick({})
    async with factory() as s:
        # everything is "recent" → nothing should be in pending state for periodic kinds
        rows = (await s.execute(
            select(Task.kind, Task.status)
            .where(Task.status == "pending")
            .where(Task.kind.in_(list(PERIODIC_INTERVALS.keys())))
        )).all()
    print("[3] re-tick within window — pending periodic kinds:", rows)
    assert rows == [], f"re-tick re-enqueued despite recency window: {rows}"

    # --- 4. recover_stuck_tasks ---------------------------------------------
    # Insert a fake "stuck" running task with expired lease.
    async with factory() as s:
        now = _now()
        stuck = Task(
            id=new_id(),
            kind="ingest_file",
            payload={"file_id": "stub-id"},
            dedup_key=None,
            status="running",
            priority=50,
            attempts=1,
            max_attempts=5,
            scheduled_at=now - timedelta(minutes=10),
            lease_expires_at=now - timedelta(seconds=120),
            locked_by="dead-worker",
            created_at=now - timedelta(minutes=15),
            started_at=now - timedelta(minutes=10),
        )
        # Also a stuck task that has exceeded retries — should be marked dead
        exhausted = Task(
            id=new_id(),
            kind="ingest_file",
            payload={"file_id": "stub-id-2"},
            dedup_key=None,
            status="running",
            priority=50,
            attempts=5,
            max_attempts=5,
            scheduled_at=now - timedelta(minutes=20),
            lease_expires_at=now - timedelta(seconds=120),
            locked_by="dead-worker",
            created_at=now - timedelta(minutes=25),
            started_at=now - timedelta(minutes=20),
        )
        s.add_all([stuck, exhausted])
        await s.commit()
        stuck_id, exhausted_id = stuck.id, exhausted.id

    await handle_recover_stuck_tasks({})

    async with factory() as s:
        s_row = await s.get(Task, stuck_id)
        e_row = await s.get(Task, exhausted_id)
        print(f"[4] stuck → status={s_row.status}, locked_by={s_row.locked_by}, lease={s_row.lease_expires_at}")
        print(f"[4] exhausted → status={e_row.status}")
        assert s_row.status == "pending"
        assert s_row.locked_by is None
        assert s_row.lease_expires_at is None
        assert e_row.status == "dead"

        kinds = (await s.execute(
            text("SELECT DISTINCT kind FROM audit_events ORDER BY kind")
        )).scalars().all()
        print("[4] audit kinds present:", kinds)
        assert "task_recovered" in kinds
        assert "task_marked_dead" in kinds

    # --- 5. prune (unified) ------------------------------------------------
    # Insert one ancient event well outside retention.
    async with factory() as s:
        ancient = AuditEvent(
            id=new_id(),
            occurred_at=_now() - timedelta(days=120),
            kind="ancient_event",
            payload={"note": "this should be pruned"},
        )
        s.add(ancient)
        await s.commit()
        ancient_id = ancient.id

    async with factory() as s:
        n_before = (await s.execute(
            text("SELECT COUNT(*) FROM audit_events")
        )).scalar()
    await handle_prune({})
    async with factory() as s:
        gone = await s.get(AuditEvent, ancient_id)
        assert gone is None, "ancient audit_events row was not pruned"
        # Unified prune writes one summary row to task_outcomes with per_target detail.
        prune_outcome_raw = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind=:k ORDER BY completed_at DESC"
        ), {"k": KIND_PRUNE})).scalars().first()
        assert prune_outcome_raw is not None
        # raw text() bypasses ORM JSON decoding on SQLite — handle both shapes.
        prune_outcome = (
            json.loads(prune_outcome_raw)
            if isinstance(prune_outcome_raw, str)
            else prune_outcome_raw
        )
        assert "per_target" in prune_outcome, prune_outcome
        assert "audit_events" in prune_outcome["per_target"], prune_outcome
        print("[5] prune outcome:", prune_outcome)

    print("\nALL DISPATCHER E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
