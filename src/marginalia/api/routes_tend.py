"""User-triggered maintenance pass — DESIGN.md §9 / §16.1.

`POST /v1/tend` enqueues a one-shot run of the librarian's maintenance
chain (tag_quality → restructure_catalogs → mine_relations →
vet_relations → propose_views → refresh_entry_extra). Returns immediately
with a run_id and the list of task ids that will execute. Progress is
queried via GET /v1/tend/{id}.

Why this exists: most users don't want to wait days for the periodic
dispatcher to run tag_quality every 6 hours. After bulk-ingesting a
batch of files, calling /tend once forces a tidy-up pass right now.

Dedup: if a periodic equivalent of any of these tasks is already
pending/running, the existing row is reused (so /tend doesn't pile up
duplicate work). Each kind's resulting task id is persisted in a
`task_outcomes` row of kind=tend_dispatch so progress lookups are O(1).
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Task
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import get_session
from marginalia.repositories import task_outcomes as task_outcomes_repo
from marginalia.repositories import tasks as tasks_repo
from marginalia.repositories.task_outcomes import record_outcome
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import (
    KIND_MINE_RELATIONS,
    KIND_PROPOSE_VIEWS,
    KIND_REFRESH_ENTRY_EXTRA,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_TAG_QUALITY,
    KIND_VET_RELATIONS,
)
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

router = APIRouter(tags=["tend"])

# Order is the priority chain from kinds.py. tag_quality first (normalize
# then enrich, both inside this kind); restructure after enrich (catalogs
# need stable tags); mine_relations runs the four miners back-to-back;
# vet_relations gates the raw graph; propose_views sees the clean graph;
# refresh closes out using everything that came before.
TEND_CHAIN: tuple[str, ...] = (
    KIND_TAG_QUALITY,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_MINE_RELATIONS,
    KIND_VET_RELATIONS,
    KIND_PROPOSE_VIEWS,
    KIND_REFRESH_ENTRY_EXTRA,
)

TEND_OBJECT_KIND = "tend_run"
TEND_DISPATCH_KIND = "tend_dispatch"

@router.post("/tend", status_code=202)
async def post_tend(
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Kick off a maintenance pass. Returns the run_id and per-kind task ids."""
    run_id = new_id()
    dispatched: list[dict[str, Any]] = []
    for kind in TEND_CHAIN:
        existing = await tasks_repo.find_pending_or_running_by_dedup(db, kind)
        if existing is not None:
            dispatched.append({
                "kind": kind,
                "task_id": existing.id,
                "skipped": True,
                "status": existing.status,
            })
            continue
        task = await enqueue(
            db,
            kind=kind,
            payload={"tend_run_id": run_id},
            dedup_key=kind,
        )
        if task is None:
            # enqueue returns None only when dedup+race lost — should not
            # happen here since we already queried, but stay robust.
            dispatched.append(
                {"kind": kind, "task_id": None, "skipped": True}
            )
            continue
        dispatched.append({
            "kind": kind,
            "task_id": task.id,
            "skipped": False,
            "status": task.status,
        })
        await audit_events_repo.append(
            db,
            kind="task_enqueued",
            task_id=task.id,
            payload={"kind": kind, "scheduled_by": "tend", "tend_run_id": run_id},
        )

    await record_outcome(
        db,
        task_kind=TEND_DISPATCH_KIND,
        object_kind=TEND_OBJECT_KIND,
        object_id=run_id,
        outcome="applied",
        detail={"chain": list(TEND_CHAIN), "dispatched": dispatched},
        task_run_id=run_id,
    )
    await db.commit()

    return {
        "tend_run_id": run_id,
        "tasks": dispatched,
    }

@router.get("/tend/{run_id}")
async def get_tend(
    run_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Look up a tend run's current state.

    Reads the dispatch row written at /tend time (which lists the task ids),
    then joins live `tasks` rows to report status/started_at/finished_at and
    any outcomes recorded with task_run_id=run_id.
    """
    dispatch = await task_outcomes_repo.find_one_by_key(
        db,
        task_kind=TEND_DISPATCH_KIND,
        object_kind=TEND_OBJECT_KIND,
        object_id=run_id,
    )
    if dispatch is None:
        raise HTTPException(status_code=404, detail="tend run not found")

    detail = dispatch.detail or {}
    dispatched = detail.get("dispatched") or []

    task_ids = [d.get("task_id") for d in dispatched if d.get("task_id")]
    tasks_by_id: dict[str, Task] = {}
    if task_ids:
        rows = await tasks_repo.list_by_ids(db, task_ids)
        tasks_by_id = {t.id: t for t in rows}

    progress: list[dict[str, Any]] = []
    state_counts = {"pending": 0, "running": 0, "done": 0, "error": 0,
                    "skipped": 0, "missing": 0}
    for d in dispatched:
        kind = d.get("kind")
        tid = d.get("task_id")
        if d.get("skipped"):
            state_counts["skipped"] += 1
            progress.append({
                "kind": kind, "task_id": None, "status": "skipped"
            })
            continue
        t = tasks_by_id.get(tid) if tid else None
        if t is None:
            # Task was pruned or never inserted; report as missing.
            state_counts["missing"] += 1
            progress.append({
                "kind": kind, "task_id": tid, "status": "missing"
            })
            continue
        status = t.status
        bucket = status if status in state_counts else "pending"
        state_counts[bucket] += 1
        progress.append({
            "kind": kind,
            "task_id": tid,
            "status": status,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None,
            "attempts": t.attempts,
            "last_error": t.last_error,
        })

    total = len(dispatched)
    settled = (
        state_counts["done"] + state_counts["error"]
        + state_counts["skipped"] + state_counts["missing"]
    )
    return {
        "tend_run_id": run_id,
        "started_at": dispatch.completed_at.isoformat()
            if dispatch.completed_at else None,
        "total": total,
        "settled": settled,
        "all_settled": settled == total,
        "state_counts": state_counts,
        "progress": progress,
    }
