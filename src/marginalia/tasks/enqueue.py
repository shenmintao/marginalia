from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import and_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models.tasks import Task
from marginalia.repositories import tasks as tasks_repo
from marginalia.tasks.kinds import DEFAULT_PRIORITIES
from marginalia.utils.ids import new_id


async def enqueue(
    session: AsyncSession,
    *,
    kind: str,
    payload: Mapping[str, Any] | None = None,
    dedup_key: str | None = None,
    priority: int | None = None,
    scheduled_at: datetime | None = None,
    max_attempts: int = 5,
) -> Task | None:
    """Enqueue a task. If `dedup_key` matches an existing pending/running row,
    skip insertion and return the existing task (or None if it cannot be reused)."""
    now = datetime.now(timezone.utc)
    if dedup_key is not None:
        existing = await tasks_repo.find_pending_or_running_by_dedup(
            session, dedup_key,
        )
        if existing is not None:
            return existing

    task_id = new_id()
    values = {
        "id": task_id,
        "kind": kind,
        "payload": dict(payload or {}),
        "dedup_key": dedup_key,
        "status": "pending",
        "priority": (
            priority if priority is not None else DEFAULT_PRIORITIES.get(kind, 100)
        ),
        "attempts": 0,
        "max_attempts": max_attempts,
        "scheduled_at": scheduled_at or now,
        "created_at": now,
    }

    dialect = session.bind.dialect.name if session.bind is not None else ""
    if dedup_key is not None and dialect in {"sqlite", "postgresql"}:
        insert_fn = sqlite_insert if dialect == "sqlite" else pg_insert
        table = Task.__table__
        active_dedup = and_(
            table.c.dedup_key.isnot(None),
            table.c.status.in_(("pending", "running")),
        )
        stmt = insert_fn(table).values(**values).on_conflict_do_nothing(
            index_elements=[table.c.dedup_key],
            index_where=active_dedup,
        )
        result = await session.execute(stmt)
        if (result.rowcount or 0) == 0:
            return await tasks_repo.find_pending_or_running_by_dedup(
                session, dedup_key,
            )
        await session.flush()
        return await session.get(Task, task_id)

    task = Task(**values)
    session.add(task)
    await session.flush()
    return task
