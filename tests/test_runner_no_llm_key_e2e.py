"""Verify the runner's no-LLM-key guard + startup sweep.

Run:
    .venv/Scripts/python tests/test_runner_no_llm_key_e2e.py

Verifies:
  1. With LLM_DEFAULT_API_KEY unset, runner.start() marks all pending
     LLM-dependent tasks dead (the "sweep") and skips bootstrap_periodic_tick.
  2. With key unset, _process refuses to dispatch an LLM-dependent kind
     even if one slips into pending after start() (e.g. via a race) —
     it goes straight to dead with the no-key error message.
  3. Non-LLM kinds (recover_stuck_tasks, prune) are NOT swept.
  4. With a key set, the sweep is a no-op.
"""
from __future__ import annotations

import os
from uuid import uuid4
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_PARENT = Path(os.environ.get("MARGINALIA_TEST_TMP", Path(__file__).resolve().parent))
_TEST_ROOT = _TEST_PARENT / f"_runner_no_llm_key_e2e_data_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = "0.1"
# Important: blank — that's the scenario under test.
os.environ.pop("LLM_DEFAULT_API_KEY", None)
os.environ["LLM_DEFAULT_API_KEY"] = ""

from marginalia.config import get_settings
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.db.session import session_scope
from marginalia.repositories import tasks as tasks_repo
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import (
    KIND_INGEST_FILE,
    KIND_MINE_RELATIONS,
    KIND_PERIODIC_TICK,
    KIND_PRUNE,
    KIND_RECOVER_STUCK_TASKS,
    KIND_TAG_QUALITY,
    LLM_DEPENDENT_KINDS,
)
from marginalia.tasks.runner import TaskRunner, _NO_LLM_KEY_ERROR


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _enqueue(kind: str, dedup: str) -> str:
    async with session_scope() as session:
        task = await enqueue(session, kind=kind, payload={}, dedup_key=dedup)
        await session.commit()
        assert task is not None
        return task.id


async def _status(task_id: str) -> tuple[str, str | None]:
    async with session_scope() as session:
        t = await tasks_repo.get(session, task_id)
        assert t is not None
        return t.status, t.last_error


async def main() -> None:
    get_settings.cache_clear()  # type: ignore[attr-defined]
    await bootstrap_schema()

    # Pre-seed: 1 LLM kind, 1 LLM kind, 2 non-LLM kinds.
    ingest_id = await _enqueue(KIND_INGEST_FILE, "ingest:test1")
    tag_id = await _enqueue(KIND_TAG_QUALITY, "tag_quality:test1")
    recover_id = await _enqueue(KIND_RECOVER_STUCK_TASKS, "recover:test1")
    prune_id = await _enqueue(KIND_PRUNE, "prune:test1")

    # --- (1) sweep -----------------------------------------------------------
    runner = TaskRunner()
    await runner.start()
    try:
        ingest_status, ingest_err = await _status(ingest_id)
        tag_status, tag_err = await _status(tag_id)
        recover_status, _ = await _status(recover_id)
        prune_status, _ = await _status(prune_id)

        assert ingest_status == "dead", f"expected dead, got {ingest_status}"
        assert _NO_LLM_KEY_ERROR in (ingest_err or ""), f"got {ingest_err!r}"
        assert tag_status == "dead", f"expected dead, got {tag_status}"
        assert recover_status == "pending", f"non-LLM kind got swept: {recover_status}"
        assert prune_status == "pending", f"non-LLM kind got swept: {prune_status}"
        print("[1] sweep marked LLM-dependent pending tasks dead, left non-LLM alone")

        # --- (2) bootstrap suppression --------------------------------------
        # bootstrap_periodic_tick must have skipped — so no periodic_tick row.
        async with session_scope() as session:
            has_tick = await tasks_repo.has_inflight_for_kind(session, KIND_PERIODIC_TICK)
        assert not has_tick, "periodic_tick should not be enqueued without an LLM key"
        print("[2] bootstrap_periodic_tick stayed quiet")

        # --- (3) dispatch guard (race scenario) -----------------------------
        # Simulate something enqueueing after start(): the runner's per-task
        # guard must catch it on dispatch even though the sweep already ran.
        # Stop the background loop first so this manual claim cannot race the
        # worker's own poll cycle on slower CI platforms.
        await runner.stop()
        late_id = await _enqueue(KIND_INGEST_FILE, "ingest:late")
        # Drive one claim+process cycle by hand instead of waiting on the loop.
        claimed = await runner._claim_batch(4)  # type: ignore[attr-defined]
        assert late_id in claimed
        for tid in claimed:
            await runner._process(tid)  # type: ignore[attr-defined]
        late_status, late_err = await _status(late_id)
        assert late_status == "dead", f"expected dead, got {late_status}"
        assert _NO_LLM_KEY_ERROR in (late_err or ""), f"got {late_err!r}"
        print("[3] dispatch guard caught late-enqueued LLM task")
    finally:
        await runner.stop()

    # --- (4) sweep is a no-op when key IS set --------------------------------
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
    get_settings.cache_clear()  # type: ignore[attr-defined]

    survives_id = await _enqueue(KIND_INGEST_FILE, "ingest:survives")
    runner2 = TaskRunner()
    # Don't await start() — we only need the sweep to run, not the loop.
    await runner2._sweep_llm_dependent_if_no_key()  # type: ignore[attr-defined]
    survives_status, _ = await _status(survives_id)
    assert survives_status == "pending", (
        f"sweep should not touch tasks when key is set; got {survives_status}"
    )
    print("[4] sweep was a no-op with api_key configured")

    # Sanity: the LLM_DEPENDENT_KINDS set covers what we tested.
    assert KIND_INGEST_FILE in LLM_DEPENDENT_KINDS
    assert KIND_TAG_QUALITY in LLM_DEPENDENT_KINDS
    assert KIND_MINE_RELATIONS not in LLM_DEPENDENT_KINDS
    assert KIND_RECOVER_STUCK_TASKS not in LLM_DEPENDENT_KINDS
    assert KIND_PRUNE not in LLM_DEPENDENT_KINDS
    print("PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"FAIL: {e}")
        sys.exit(1)
