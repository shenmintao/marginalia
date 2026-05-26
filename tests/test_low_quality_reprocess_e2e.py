"""Periodic self-heal: re-ingest files whose summary is empty/short.

The contract being locked in:
  1. A live, ingested File whose `summary` is shorter than 50 chars (or
     NULL) is picked up by `_dispatch_reprocess_low_quality` on the next
     periodic_tick fire.
  2. The dispatcher uses the shared `services.reprocess.reprocess_file`
     primitive — same effect as a user clicking Reprocess in the GUI.
  3. A `task_outcomes` row keyed (reprocess_low_quality, file, file_id)
     is recorded so the next tick within LOW_QUALITY_COOLDOWN skips it.
  4. Files with healthy summaries (>= 50 chars) are not touched.
  5. Files that have never been ingested (`ingested_at IS NULL`) are not
     touched — they belong to the normal ingest pipeline + recover_stuck.

Run:
    .venv/Scripts/python tests/test_low_quality_reprocess_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_low_quality_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, TaskOutcome
from marginalia.tasks.handlers.periodic_tick import (
    LOW_QUALITY_OUTCOME_KIND,
    _dispatch_reprocess_low_quality,
)
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.utils.ids import new_id


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _insert_file(
    *, summary: str | None, ingested: bool, deleted: bool = False,
    ingested_offset_seconds: int = 0,
) -> str:
    """Hand-craft a File row in the state we want — bypasses the upload
    pipeline so the test is hermetic."""
    factory = get_session_factory()
    fid = new_id()
    now = _utcnow()
    async with factory() as s:
        row = File(
            id=fid,
            sha256=f"sha-{fid}",
            storage_key=f"local/{fid}",
            mime_type="text/markdown",
            size_bytes=10,
            summary=summary,
            ingest_status="done" if ingested else "pending",
            ingested_at=(
                now - timedelta(seconds=ingested_offset_seconds)
                if ingested else None
            ),
            deleted_at=now if deleted else None,
        )
        s.add(row)
        await s.commit()
    return fid


async def main() -> None:
    await _create_schema()
    factory = get_session_factory()

    # The fake_low fixture set:
    #   bad_short  : "abc" — picked up
    #   bad_empty  : NULL  — picked up
    #   ok         : 80-char summary — left alone
    #   not_yet    : ingested_at IS NULL — left alone (different pipeline)
    #   deleted    : soft-deleted — left alone
    bad_short = await _insert_file(
        summary="abc", ingested=True, ingested_offset_seconds=300,
    )
    bad_empty = await _insert_file(
        summary=None, ingested=True, ingested_offset_seconds=200,
    )
    ok = await _insert_file(
        summary=("real summary " * 6).strip(), ingested=True,
        ingested_offset_seconds=100,
    )
    not_yet = await _insert_file(summary=None, ingested=False)
    deleted = await _insert_file(
        summary="x", ingested=True, deleted=True, ingested_offset_seconds=50,
    )

    # ---- first dispatch ----
    async with factory() as s:
        enqueued = await _dispatch_reprocess_low_quality(s, _utcnow())
        await s.commit()

    assert set(enqueued) == {bad_short, bad_empty}, (
        f"expected only bad_short/bad_empty enqueued, got {enqueued}"
    )
    print("[1] dispatch picked exactly the two bad-summary files")

    # ---- check the side effects ----
    async with factory() as s:
        # bad_short / bad_empty: ingest state cleared, new ingest_file task
        for fid in (bad_short, bad_empty):
            row = await s.get(File, fid)
            assert row.ingested_at is None, f"{fid}: ingested_at should be NULL"
            assert row.ingest_status == "pending", \
                f"{fid}: status should be pending, got {row.ingest_status!r}"
            tasks = (await s.execute(text(
                "SELECT id FROM tasks WHERE kind = :k AND payload LIKE :p"
            ), {"k": KIND_INGEST_FILE, "p": f'%\"{fid}\"%'})).all()
            assert len(tasks) == 1, f"{fid}: expected 1 ingest task, got {len(tasks)}"

        # ok / not_yet / deleted: untouched
        for fid, label in ((ok, "ok"), (not_yet, "not_yet"), (deleted, "deleted")):
            row = await s.get(File, fid)
            tasks = (await s.execute(text(
                "SELECT id FROM tasks WHERE kind = :k AND payload LIKE :p"
            ), {"k": KIND_INGEST_FILE, "p": f'%\"{fid}\"%'})).all()
            assert len(tasks) == 0, f"{label}: should not have an ingest task"

        # task_outcomes recorded for the two bad files only
        outcomes = (await s.execute(
            select(TaskOutcome).where(
                TaskOutcome.task_kind == LOW_QUALITY_OUTCOME_KIND,
            )
        )).scalars().all()
        recorded = {o.object_id for o in outcomes}
        assert recorded == {bad_short, bad_empty}, \
            f"task_outcomes object_ids: {recorded}"
        for o in outcomes:
            assert o.outcome == "applied", \
                f"{o.object_id}: outcome should be 'applied', got {o.outcome!r}"
    print("[2] state reset, ingest tasks enqueued, task_outcomes written (bad files only)")

    # ---- cooldown: a second tick within 24h should skip both ----
    # bad_empty was just dispatched and its summary is still NULL — without
    # cooldown it would re-enqueue forever (10-min churn). Cooldown saves us.
    async with factory() as s:
        enqueued2 = await _dispatch_reprocess_low_quality(s, _utcnow())
        await s.commit()
    assert enqueued2 == [], (
        f"second tick within cooldown should skip everything, got {enqueued2}"
    )
    print("[3] within cooldown: dispatcher skipped both files (no churn)")

    # ---- after cooldown + re-ingest produced another bad summary ----
    # Real flow: worker picks up the enqueued ingest_file, runs the
    # pipeline, writes a NEW summary, sets ingested_at. If that new
    # summary is also bad, the next tick (after cooldown) should pick it
    # up again. Simulate that here without spinning up the worker:
    # mark the prior ingest_file rows done, restore ingested_at, leave
    # the bad summaries untouched, and backdate the task_outcomes.
    async with factory() as s:
        await s.execute(text(
            "UPDATE tasks SET status='done', finished_at=:now "
            "WHERE kind=:k"
        ), {"now": _utcnow(), "k": KIND_INGEST_FILE})
        await s.execute(text(
            "UPDATE files SET ingested_at = :now, ingest_status='done', "
            "summary = :sum WHERE id IN (:a, :b)"
        ), {
            "now": _utcnow(), "sum": "still bad",
            "a": bad_short, "b": bad_empty,
        })
        await s.execute(text(
            "UPDATE task_outcomes SET completed_at = :t "
            "WHERE task_kind = :k"
        ), {
            "t": _utcnow() - timedelta(hours=48),
            "k": LOW_QUALITY_OUTCOME_KIND,
        })
        await s.commit()

    async with factory() as s:
        enqueued3 = await _dispatch_reprocess_low_quality(s, _utcnow())
        await s.commit()
    assert set(enqueued3) == {bad_short, bad_empty}, (
        f"after cooldown + still-bad summary, expected both files re-enqueued, "
        f"got {enqueued3}"
    )
    print("[4] after cooldown: bad files re-enqueued for another reprocess pass")

    print("\nALL LOW-QUALITY REPROCESS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
