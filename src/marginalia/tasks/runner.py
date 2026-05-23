from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from marginalia.config import Settings, get_settings
from marginalia.db.models.tasks import Task
from marginalia.db.session import session_scope
from marginalia.tasks import handlers as _handlers_pkg  # noqa: F401  (register)
from marginalia.tasks.kinds import get_handler

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int) -> timedelta:
    base = min(60 * (2 ** max(0, attempts - 1)), 60 * 60)
    return timedelta(seconds=base)


class TaskRunner:
    """In-process async worker. Polls `tasks` table, claims rows, runs handlers."""

    def __init__(self, settings: Settings | None = None, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        from marginalia.tasks.handlers.periodic_tick import bootstrap_periodic_tick
        await bootstrap_periodic_tick()
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run(), name="marginalia.task_runner")

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            await self._loop_task
            self._loop_task = None
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    async def _run(self) -> None:
        log.info("TaskRunner %s starting", self.worker_id)
        while not self._stop.is_set():
            try:
                claimed = await self._claim_batch(self.settings.worker_batch_size)
            except Exception:
                log.exception("claim batch failed")
                claimed = []
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.worker_poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            for task_id in claimed:
                t = asyncio.create_task(self._process(task_id))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)
        log.info("TaskRunner %s stopped", self.worker_id)

    async def _claim_batch(self, limit: int) -> list[str]:
        now = _now()
        lease_until = now + timedelta(seconds=self.settings.worker_lease_seconds)
        async with session_scope() as session:
            if self.settings.db_backend == "postgres":
                rows = (
                    await session.execute(
                        select(Task.id)
                        .where(
                            Task.status == "pending",
                            Task.scheduled_at <= now,
                        )
                        .order_by(Task.priority.asc(), Task.scheduled_at.asc())
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()
            else:
                rows = (
                    await session.execute(
                        select(Task.id)
                        .where(
                            Task.status == "pending",
                            Task.scheduled_at <= now,
                        )
                        .order_by(Task.priority.asc(), Task.scheduled_at.asc())
                        .limit(limit)
                    )
                ).scalars().all()
            if not rows:
                await session.commit()
                return []
            await session.execute(
                update(Task)
                .where(Task.id.in_(rows), Task.status == "pending")
                .values(
                    status="running",
                    locked_by=self.worker_id,
                    lease_expires_at=lease_until,
                    started_at=now,
                    attempts=Task.attempts + 1,
                )
            )
            await session.commit()
            return list(rows)

    async def _process(self, task_id: str) -> None:
        async with session_scope() as session:
            task = await session.get(Task, task_id)
            if task is None or task.status != "running":
                return
            handler = get_handler(task.kind)
            payload = dict(task.payload or {})
            attempts = task.attempts
            max_attempts = task.max_attempts
            kind = task.kind

        if handler is None:
            await self._fail(task_id, attempts, max_attempts, f"no handler registered for {kind!r}")
            return

        heartbeat = asyncio.create_task(self._heartbeat(task_id))
        try:
            await handler(payload)
        except Exception as exc:
            heartbeat.cancel()
            log.exception("task %s (%s) failed", task_id, kind)
            await self._fail(task_id, attempts, max_attempts, repr(exc))
            return
        finally:
            heartbeat.cancel()

        async with session_scope() as session:
            await session.execute(
                update(Task)
                .where(Task.id == task_id)
                .values(
                    status="done",
                    finished_at=_now(),
                    last_error=None,
                    lease_expires_at=None,
                    locked_by=None,
                )
            )
            await session.commit()

    async def _heartbeat(self, task_id: str) -> None:
        interval = self.settings.worker_heartbeat_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                async with session_scope() as session:
                    await session.execute(
                        update(Task)
                        .where(Task.id == task_id, Task.status == "running")
                        .values(
                            lease_expires_at=_now()
                            + timedelta(seconds=self.settings.worker_lease_seconds)
                        )
                    )
                    await session.commit()
        except asyncio.CancelledError:
            return

    async def _fail(
        self, task_id: str, attempts: int, max_attempts: int, error: str
    ) -> None:
        async with session_scope() as session:
            if attempts >= max_attempts:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status="dead",
                        last_error=error,
                        finished_at=_now(),
                        lease_expires_at=None,
                        locked_by=None,
                    )
                )
            else:
                await session.execute(
                    update(Task)
                    .where(Task.id == task_id)
                    .values(
                        status="pending",
                        last_error=error,
                        scheduled_at=_now() + _backoff(attempts),
                        lease_expires_at=None,
                        locked_by=None,
                    )
                )
            await session.commit()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runner = TaskRunner()
    await runner.start()
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.stop()


def _try_recover_stale(unused_iterable: Iterable[None] = ()) -> None:
    """Reserved for future: rescue running rows whose lease expired before their
    worker could finish (worker crashed). Implementation deferred."""
    return None


if __name__ == "__main__":
    asyncio.run(main())
