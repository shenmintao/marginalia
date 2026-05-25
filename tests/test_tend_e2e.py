"""End-to-end test for /v1/tend (user-triggered maintenance pass).

Run:
    .venv/Scripts/python tests/test_tend_e2e.py

Verifies:
  1. POST /v1/tend enqueues all 6 kinds in TEND_CHAIN with priority + dedup
     reused if a periodic kind is already in flight.
  2. Each enqueued task has payload['tend_run_id'] = run_id.
  3. A `task_outcomes` dispatch row is written with task_kind=tend_dispatch
     and detail.dispatched listing every (kind, task_id, skipped) tuple.
  4. GET /v1/tend/{run_id} reports per-kind status + state counts.
  5. After tasks settle, all_settled flips to True.
  6. A second POST /v1/tend within the dedup window reuses pending rows
     and reports skipped=true for them.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_tend_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

import httpx
from httpx import ASGITransport

from marginalia.api.routes_tend import TEND_CHAIN, TEND_DISPATCH_KIND, TEND_OBJECT_KIND
from marginalia.db.engine import get_engine
from marginalia.db.models import Base, Task
from marginalia.db.models.task_outcomes import TaskOutcome
from marginalia.db.session import session_scope
from marginalia.main import app


async def _setup_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _go() -> None:
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 1. POST /v1/tend kicks off the chain.
            r = await c.post("/v1/tend")
            assert r.status_code == 202, r.text
            out = r.json()
            run_id = out["tend_run_id"]
            print(f"[1] tend run started: {run_id}")
            assert len(out["tasks"]) == len(TEND_CHAIN)
            assert all(not t["skipped"] for t in out["tasks"])
            for got, want in zip(out["tasks"], TEND_CHAIN):
                assert got["kind"] == want, f"chain mismatch at {got}"

            # 2. Each task carries tend_run_id in payload.
            task_ids = [t["task_id"] for t in out["tasks"]]
            async with session_scope() as db:
                rows = (
                    await db.execute(select(Task).where(Task.id.in_(task_ids)))
                ).scalars().all()
                assert len(rows) == len(TEND_CHAIN)
                for t in rows:
                    assert t.payload.get("tend_run_id") == run_id, (
                        f"task {t.id} missing tend_run_id"
                    )
            print(f"[2] all {len(rows)} tasks have payload.tend_run_id={run_id}")

            # 3. Dispatch outcome row exists.
            async with session_scope() as db:
                dispatch = (
                    await db.execute(
                        select(TaskOutcome).where(
                            TaskOutcome.task_kind == TEND_DISPATCH_KIND,
                            TaskOutcome.object_kind == TEND_OBJECT_KIND,
                            TaskOutcome.object_id == run_id,
                        )
                    )
                ).scalar_one_or_none()
                assert dispatch is not None
                assert dispatch.task_run_id == run_id
                assert dispatch.outcome == "applied"
                detail = dispatch.detail or {}
                assert "dispatched" in detail
                assert len(detail["dispatched"]) == len(TEND_CHAIN)
            print("[3] tend_dispatch outcome row recorded with full chain detail")

            # 4. GET /v1/tend/{run_id} reports per-kind status.
            r = await c.get(f"/v1/tend/{run_id}")
            assert r.status_code == 200, r.text
            status = r.json()
            print(f"[4] tend status: total={status['total']}, "
                  f"settled={status['settled']}, all_settled={status['all_settled']}")
            assert status["tend_run_id"] == run_id
            assert status["total"] == len(TEND_CHAIN)
            assert status["all_settled"] is False  # tasks not run yet
            assert status["state_counts"]["pending"] == len(TEND_CHAIN)

            # 5. Drive tasks to terminal state to confirm settled accounting.
            async with session_scope() as db:
                rows = (
                    await db.execute(select(Task).where(Task.id.in_(task_ids)))
                ).scalars().all()
                now = datetime.now(timezone.utc)
                for t in rows:
                    t.status = "done"
                    t.started_at = now
                    t.finished_at = now
                await db.commit()
            r = await c.get(f"/v1/tend/{run_id}")
            status = r.json()
            assert status["all_settled"] is True, status
            assert status["state_counts"]["done"] == len(TEND_CHAIN)
            print(f"[5] after settlement, all_settled=True, done={status['state_counts']['done']}")

            # 6. A second POST /v1/tend reuses no pending tasks (they all
            #    moved to done, so none are dedup-reused). Try the dedup
            #    branch by re-running once more without resetting.
            r = await c.post("/v1/tend")
            assert r.status_code == 202, r.text
            second = r.json()
            run2 = second["tend_run_id"]
            assert run2 != run_id
            # All chain entries are in 'done' status; new run reaches none of
            # them as pending, so nothing is skipped — fresh tasks for all.
            print(f"[6] second tend run {run2}: "
                  f"{sum(1 for t in second['tasks'] if not t['skipped'])} fresh, "
                  f"{sum(1 for t in second['tasks'] if t['skipped'])} skipped")
            assert all(t["task_id"] is not None for t in second["tasks"])

            # 7. Now exercise the skip branch: a third request immediately
            #    after the second should hit dedup since pending rows exist.
            r = await c.post("/v1/tend")
            third = r.json()
            run3 = third["tend_run_id"]
            assert run3 != run2
            skipped_in_third = sum(1 for t in third["tasks"] if t["skipped"])
            assert skipped_in_third == len(TEND_CHAIN), (
                f"expected all {len(TEND_CHAIN)} skipped, got {skipped_in_third}"
            )
            print(f"[7] third tend run {run3}: all {skipped_in_third} dedup-skipped")


def main() -> int:
    asyncio.run(_setup_schema())
    asyncio.run(_go())
    print("\nALL TEND E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
