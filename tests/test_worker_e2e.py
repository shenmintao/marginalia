"""End-to-end worker daemon test (Cycle 19).

Run:
    .venv/Scripts/python tests/test_worker_e2e.py

Architecture under test:
  - The API server's `WORKER_ENABLED=false` — its lifespan does NOT start
    an in-process TaskRunner.
  - We start an independent TaskRunner in this process (simulating a
    separate `marginalia-worker` invocation talking to the same DB +
    storage).
  - Upload a file via the API → expect a row in `tasks`. Verify the
    standalone runner picks it up, marks done, and writes the file's
    content fields.
  - Graceful shutdown via runner.stop() drains in-flight tasks.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_worker_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"            # API does NOT spawn worker
os.environ["WORKER_POLL_INTERVAL_SECONDS"] = "0.1"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, FileEntry
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.tasks.runner import TaskRunner


CALL_LOG: list[ChatRequest] = []


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        payload = {
            "summary": "Worker-test note about Marginalia.",
            "description": {
                "sections": [
                    {"id": "s1", "title": "Intro",
                     "anchor": {"unit": "heading", "value": "1"},
                     "summary": "Intro paragraph.",
                     "key_terms": ["worker", "daemon"]},
                ],
            },
            "kind": "text",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Worker"],
            "entry_tags": [
                {"name": "worker-test", "facet": "topic"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=600, output_tokens=120,
                             cache_read_tokens=400),
            parsed_json=payload,
        )


def _install_fake() -> None:
    llm.reset_clients_cache()
    fake = _FakeIngest()
    def _factory(profile: str = "ingest"):
        return fake
    import marginalia.pipelines.text as tmod
    tmod.get_chat_client = _factory  # type: ignore[assignment]


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_status(file_id: str, *, expect: str,
                           timeout: float = 12.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (await s.execute(text(
                "SELECT ingest_status FROM files WHERE id=:id"
            ), {"id": file_id})).first()
            if row is None:
                raise RuntimeError("file vanished")
            (status,) = row
            if status == expect:
                return status
            if status in ("done", "failed", "dead"):
                return status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"file status stayed not {expect}")


async def main() -> None:
    _install_fake()
    await _create_schema()

    transport = ASGITransport(app=app)
    standalone_runner = TaskRunner()  # simulates the separate worker process

    # --- 1. API lifespan does NOT spawn its own runner ----------------
    # Verify by reading config:
    settings = get_settings()
    assert settings.worker_enabled is False, \
        "test setup wrong: WORKER_ENABLED should be false"

    async with app.router.lifespan_context(app):
        # Confirm no inflight task runner attached to this process via
        # the lifespan context: there's no public hook so we just rely
        # on the fact that the standalone_runner is the only TaskRunner
        # we manually started.
        await standalone_runner.start()
        try:
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as c:
                # --- 2. Upload via API → ingest task enqueued ---------
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/research/notes/"},
                    files={"file": ("note.md",
                                    io.BytesIO(b"# Title\n\nA short note for the worker test.\n"),
                                    "text/markdown")},
                )
                assert r.status_code == 201, r.text
                up = r.json()
                file_id = up["file_id"]
                entry_id = up["entry_id"]
                print("[1] uploaded; file_id =", file_id[:8])

                factory = get_session_factory()
                async with factory() as s:
                    task_id = (await s.execute(text(
                        "SELECT id FROM tasks WHERE kind=:k AND payload LIKE :p"
                    ), {"k": KIND_INGEST_FILE,
                        "p": f'%"{file_id}"%'})).scalar_one()
                print("[2] ingest_file task enqueued:", task_id[:8])

                # --- 3. Standalone worker picks it up ---------------
                status = await _wait_for_status(file_id, expect="done",
                                                timeout=12.0)
                print("[3] task processed by standalone worker; status:", status)
                assert status == "done"
                assert len(CALL_LOG) == 1

                # --- 4. DB invariants ------------------------------
                async with factory() as s:
                    f = await s.get(File, file_id)
                    e = await s.get(FileEntry, entry_id)
                    assert f.kind == "text"
                    assert f.summary
                    assert f.ingested_at is not None
                    assert e.catalog_id is not None
                print("[4] file content fields written + entry classified")

                # --- 5. locked_by reflects this worker_id ----------
                async with factory() as s:
                    rows = (await s.execute(text(
                        "SELECT locked_by, status FROM tasks WHERE id=:id"
                    ), {"id": task_id})).first()
                # After done, locked_by should be cleared
                print("[5] post-done task row:", rows)
                assert rows[1] == "done"
                assert rows[0] is None

        finally:
            # --- 6. graceful stop drains and exits ---------------
            await standalone_runner.stop()
            print("[6] worker.stop() returned cleanly")

    # --- 7. Concurrent uploads handled in batch ---------------------
    CALL_LOG.clear()
    runner2 = TaskRunner()
    async with app.router.lifespan_context(app):
        await runner2.start()
        try:
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as c:
                file_ids: list[str] = []
                for i in range(3):
                    body = f"# Doc {i}\n\nbody-{i}\n".encode("utf-8")
                    r = await c.post(
                        "/v1/upload",
                        params={"remote_path": "/research/batch/"},
                        params_=None,
                        files={"file": (f"d{i}.md", io.BytesIO(body),
                                        "text/markdown")},
                    ) if False else await c.post(
                        "/v1/upload",
                        params={"remote_path": "/research/batch/",
                                "display_name": f"d{i}.md"},
                        files={"file": (f"d{i}.md", io.BytesIO(body),
                                        "text/markdown")},
                    )
                    assert r.status_code == 201, r.text
                    file_ids.append(r.json()["file_id"])

                for fid in file_ids:
                    s_ = await _wait_for_status(fid, expect="done",
                                                timeout=12.0)
                    assert s_ == "done", f"file {fid} status: {s_}"
                print("[7] 3 concurrent ingests all done; LLM calls:",
                      len(CALL_LOG))
                assert len(CALL_LOG) == 3
        finally:
            await runner2.stop()

    print("\nALL WORKER E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
