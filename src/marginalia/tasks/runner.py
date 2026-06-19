from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone

from marginalia.config import LlmConfigError, Settings, get_settings, validate_llm_config
from marginalia.db.session import session_scope
from marginalia.repositories import tasks as tasks_repo
from marginalia.repositories import task_outcomes as outcomes_repo
from marginalia.services.ingest_status import mark_file_failed_for_dead_ingest_task
from marginalia.tasks import handlers as _handlers_pkg  # noqa: F401  (register)
from marginalia.tasks.kinds import LLM_DEPENDENT_KINDS, get_handler
from marginalia.tasks.usage import (
    bind_accumulator, unbind_accumulator, UsageCounters,
)

log = logging.getLogger(__name__)


_NO_LLM_KEY_ERROR = (
    "skipped: no LLM api_key configured. "
    "Set LLM_DEFAULT_API_KEY in .env or in Settings → LLM Profile, then restart."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int) -> timedelta:
    base = min(60 * (2 ** max(0, attempts - 1)), 60 * 60)
    return timedelta(seconds=base)


class TaskRunner:
    """In-process async worker. Polls `tasks` table, claims rows, runs handlers."""

    def __init__(self, settings: Settings | None = None, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self._static_settings = settings is not None
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        await self._sweep_llm_dependent_if_no_key()
        from marginalia.tasks.handlers.periodic_tick import bootstrap_periodic_tick
        await bootstrap_periodic_tick()
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run(), name="marginalia.task_runner")

    def _has_llm_key(self) -> bool:
        try:
            validate_llm_config(self._current_settings())
        except LlmConfigError:
            return False
        return True

    async def _sweep_llm_dependent_if_no_key(self) -> None:
        """At startup, if no api_key is configured, mark all pending
        LLM-dependent tasks dead. Otherwise the runner picks them up
        within seconds and they fail one by one with OpenAIError noise.

        Runs once per start(). The user's path back is: set a key,
        restart the app — bootstrap_periodic_tick will then re-enqueue
        the periodic kinds normally on the next startup."""
        if self._has_llm_key():
            return
        async with session_scope() as session:
            pending = await tasks_repo.list_pending_by_kinds(
                session,
                kinds=sorted(LLM_DEPENDENT_KINDS),
            )
            n = await tasks_repo.mark_pending_dead_by_kinds(
                session,
                kinds=sorted(LLM_DEPENDENT_KINDS),
                now=_now(),
                error=_NO_LLM_KEY_ERROR,
            )
            for task in pending:
                await mark_file_failed_for_dead_ingest_task(
                    session,
                    task_id=task.id,
                    kind=task.kind,
                    payload=task.payload or {},
                    reason=_NO_LLM_KEY_ERROR,
                )
            await session.commit()
        if n:
            log.warning(
                "marked %d pending LLM-dependent task(s) dead: no api_key configured",
                n,
            )

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
            settings = self._current_settings()
            max_concurrent = max(1, int(settings.worker_batch_size or 1))
            available = max_concurrent - len(self._inflight)
            if available <= 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                claimed = await self._claim_batch(available)
            except Exception:
                log.exception("claim batch failed")
                claimed = []
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            for task_id in claimed:
                t = asyncio.create_task(self._process(task_id))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)
        log.info("TaskRunner %s stopped", self.worker_id)

    def _current_settings(self) -> Settings:
        if self._static_settings:
            return self.settings
        return get_settings()

    async def _claim_batch(self, limit: int) -> list[str]:
        now = _now()
        settings = self._current_settings()
        lease_until = now + timedelta(seconds=settings.worker_lease_seconds)
        async with session_scope() as session:
            rows = await tasks_repo.claim_pending_ids(
                session,
                now=now,
                limit=limit,
            )
            if not rows:
                await session.commit()
                return []
            claimed = await tasks_repo.mark_running(
                session,
                ids=rows,
                now=now,
                lease_until=lease_until,
                worker_id=self.worker_id,
            )
            await session.commit()
            return list(claimed)

    async def _process(self, task_id: str) -> None:
        async with session_scope() as session:
            task = await tasks_repo.get(session, task_id)
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

        # Guard: if this kind needs LLM and no key is configured, mark
        # dead immediately with a clean message instead of letting the
        # handler crash with `OpenAIError: Missing credentials`. Catches
        # rows queued before bootstrap was guarded, plus anything
        # enqueued by future code paths that forget to check first.
        if kind in LLM_DEPENDENT_KINDS and not self._has_llm_key():
            await self._fail(task_id, max_attempts, max_attempts, _NO_LLM_KEY_ERROR)
            return

        token = bind_accumulator()
        started = time.monotonic()
        heartbeat = asyncio.create_task(self._heartbeat(task_id))
        try:
            await handler(payload)
        except Exception as exc:
            heartbeat.cancel()
            log.exception("task %s (%s) failed", task_id, kind)
            duration_ms = int((time.monotonic() - started) * 1000)
            counters = unbind_accumulator(token) or UsageCounters()
            changed = await self._fail(task_id, attempts, max_attempts, repr(exc))
            if changed:
                await self._record_outcome(
                    task_id=task_id, kind=kind, outcome="error",
                    counters=counters, duration_ms=duration_ms,
                )
            return
        finally:
            heartbeat.cancel()

        duration_ms = int((time.monotonic() - started) * 1000)
        counters = unbind_accumulator(token) or UsageCounters()
        async with session_scope() as session:
            changed = await tasks_repo.mark_done(
                session,
                task_id=task_id,
                now=_now(),
                worker_id=self.worker_id,
            )
            if changed:
                await outcomes_repo.record_outcome(
                    session,
                    task_kind=kind,
                    object_kind="task",
                    object_id=task_id,
                    outcome="applied",
                    detail=counters.to_detail(duration_ms=duration_ms),
                )
            await session.commit()

    async def _record_outcome(
        self, *, task_id: str, kind: str, outcome: str,
        counters: UsageCounters, duration_ms: int,
    ) -> None:
        try:
            async with session_scope() as session:
                await outcomes_repo.record_outcome(
                    session,
                    task_kind=kind,
                    object_kind="task",
                    object_id=task_id,
                    outcome=outcome,
                    detail=counters.to_detail(duration_ms=duration_ms),
                )
                await session.commit()
        except Exception:
            log.exception("failed to record task_outcome for %s", task_id)

    async def _heartbeat(self, task_id: str) -> None:
        try:
            while True:
                settings = self._current_settings()
                interval = settings.worker_heartbeat_seconds
                await asyncio.sleep(interval)
                async with session_scope() as session:
                    now = _now()
                    await tasks_repo.heartbeat(
                        session,
                        task_id=task_id,
                        lease_until=now + timedelta(
                            seconds=settings.worker_lease_seconds,
                        ),
                        now=now,
                    )
                    await session.commit()
        except asyncio.CancelledError:
            return

    async def _fail(
        self, task_id: str, attempts: int, max_attempts: int, error: str
    ) -> bool:
        async with session_scope() as session:
            task = await tasks_repo.get(session, task_id)
            if attempts >= max_attempts:
                changed = await tasks_repo.mark_dead(
                    session,
                    task_id=task_id,
                    now=_now(),
                    error=error,
                    worker_id=self.worker_id,
                )
                if changed and task is not None:
                    await mark_file_failed_for_dead_ingest_task(
                        session,
                        task_id=task.id,
                        kind=task.kind,
                        payload=task.payload or {},
                        reason=error,
                    )
            else:
                changed = await tasks_repo.reschedule_for_retry(
                    session,
                    task_id=task_id,
                    error=error,
                    next_run_at=_now() + _backoff(attempts),
                    worker_id=self.worker_id,
                )
            await session.commit()
            return changed


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runner = TaskRunner()
    await runner.start()
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
