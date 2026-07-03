"""marginalia-worker — standalone task worker daemon.

Usage:
    marginalia-worker                    # use config from environment / .env
    marginalia-worker --log-level DEBUG  # override

Architecture:
    Production deploys run the API (uvicorn) and the worker as separate
    processes that share the database + storage. The API process should
    be configured with WORKER_ENABLED=false; this entrypoint runs the
    same TaskRunner used by the in-process mode but in its own loop with
    proper signal handling.

Signals:
    SIGINT / SIGTERM trigger graceful shutdown: stop claiming new tasks,
    wait for in-flight handlers, then exit. SIGKILL bypasses cleanup —
    the next worker that comes up will be cleaned by recover_stuck_tasks.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from marginalia.config import Settings, get_settings
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.tasks.runner import TaskRunner

log = logging.getLogger("marginalia.worker")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


async def _arun(settings: Settings) -> int:
    await bootstrap_schema()
    runner = TaskRunner(settings=settings)
    await runner.start()
    log.info(
        "worker %s started; polling every %.1fs "
        "(lease=%ds, concurrent_tasks=%d)",
        runner.worker_id,
        settings.worker_poll_interval_seconds,
        settings.worker_lease_seconds,
        settings.worker_batch_size,
    )

    stop_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        if stop_event.is_set():
            log.warning("second %s received; force-quitting", signame)
            sys.exit(1)
        log.info("received %s; draining in-flight tasks…", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _on_signal, sig_name)
        except NotImplementedError:
            # Windows doesn't implement add_signal_handler. Under
            # asyncio.run a raw Ctrl-C would propagate KeyboardInterrupt
            # out of run_forever and skip runner.stop() entirely, so fall
            # back to signal.signal with a handler that hops onto the
            # loop — keeping the graceful drain path below reachable.
            def _sync_handler(signum, frame, _name=sig_name):
                loop.call_soon_threadsafe(_on_signal, _name)
            try:
                signal.signal(sig, _sync_handler)
            except (ValueError, OSError):
                pass  # not on the main thread / signal unsupported

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt; draining in-flight tasks…")

    await runner.stop()
    log.info("worker stopped cleanly.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="marginalia-worker",
                                     description="Marginalia task worker.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()
    _setup_logging(args.log_level)

    settings = get_settings()
    try:
        return asyncio.run(_arun(settings))
    except KeyboardInterrupt:
        log.info("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
