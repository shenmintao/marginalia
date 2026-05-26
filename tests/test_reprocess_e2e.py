"""End-to-end reprocess sanity check.

The reprocess primitive lets a user say "AI got smarter, redo this." It
clears the write-once gate (ingested_at = NULL), purges entry_tags, and
re-enqueues KIND_INGEST_FILE. This test locks in:

  1. After upload + first ingest, summary/tags are populated.
  2. POST /v1/files/{file_id}/reprocess clears state and enqueues a new
     ingest_file task.
  3. The runner re-runs the pipeline. The new (different) summary
     overwrites the old one — write-once gate is broken.
  4. Old entry_tags are gone; new ones from the second run are present.
  5. Bulk reprocess by file_ids enqueues for every listed file.
  6. Bulk by `all=true` covers live files only (skips deleted).
  7. Body validation: 422 on zero or multiple filters.

Run:
    .venv/Scripts/python tests/test_reprocess_e2e.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_reprocess_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, EntryTag, File, FileEntry, Tag,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.tasks.runner import TaskRunner


# Two canned LLM payloads — first vs. reprocessed. Different summary
# text, different tag set, so we can prove "AI overwrote".
def _payload(summary: str, tag_name: str) -> dict:
    return {
        "summary": summary,
        "description": {
            "sections": [{
                "id": "s1", "title": "Overview",
                "anchor": {"unit": "heading", "path": "1"},
                "summary": "intro", "key_terms": ["x"],
            }],
        },
        "kind": "text",
        "extra": "themes",
        "entry_extra": "context",
        "entry_catalog_path": ["Notes"],
        "entry_tags": [
            {"name": tag_name, "facet": "topic"},
            {"name": "english", "facet": "language"},
        ],
    }


CALL_LOG: list[ChatRequest] = []


class _FakeChatClient:
    """Returns canned ingest payloads from a FIFO queue. If the queue is
    empty (e.g., a periodic handler grabs the client), returns a benign
    no-op payload so the test doesn't depend on which handler runs."""
    profile_name = "ingest"
    model = "fake-model"

    def __init__(self) -> None:
        self.responses: list[dict] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        if self.responses:
            payload = self.responses.pop(0)
        else:
            # Inert payload — won't disturb periodic handlers; matches
            # the ingest schema loosely. If a real ingest call hits this,
            # the assertion on summary text will catch the mistake.
            payload = {
                "summary": "(noop)",
                "description": {"sections": []},
                "kind": "text",
                "extra": "",
                "entry_extra": "",
                "entry_catalog_path": [],
                "entry_tags": [],
                "operations": [],
            }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=50, cache_read_tokens=0),
            parsed_json=payload,
        )


_FAKE = _FakeChatClient()


def _install_fake_llm() -> None:
    llm.reset_clients_cache()
    llm.factory.get_chat_client.cache_clear()  # type: ignore[attr-defined]
    def _fake_factory(profile: str = "ingest"):
        return _FAKE
    llm.factory.get_chat_client = _fake_factory  # type: ignore[assignment]
    # The periodic tick may schedule restructure/normalize/etc. while
    # this test is running; each of those imported `get_chat_client`
    # at module load. Patch them all so nothing tries the real network.
    import marginalia.pipelines.text as text_mod
    text_mod.get_chat_client = _fake_factory  # type: ignore[assignment]
    for mod_name in (
        "marginalia.tasks.handlers.restructure_catalogs",
        "marginalia.tasks.handlers.normalize_tags",
        "marginalia.tasks.handlers.enrich_tags",
        "marginalia.tasks.handlers.propose_views",
        "marginalia.tasks.handlers.refresh_entry_extra",
        "marginalia.tasks.handlers.vet_relations",
        "marginalia.tasks.handlers.summarize_session",
        "marginalia.tasks.handlers.mine_corpus_evidence",
    ):
        try:
            mod = __import__(mod_name, fromlist=["get_chat_client"])
        except ImportError:
            continue
        if hasattr(mod, "get_chat_client"):
            mod.get_chat_client = _fake_factory  # type: ignore[assignment]


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_task_done(task_id: str, timeout: float = 10.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (
                await s.execute(text(
                    "SELECT status FROM tasks WHERE id = :id"
                ), {"id": task_id})
            ).first()
            if row is None:
                raise RuntimeError(f"task {task_id} disappeared")
            if row[0] in ("done", "dead"):
                return row[0]
        await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id} did not finish")


async def _latest_ingest_task_id(file_id: str) -> str:
    factory = get_session_factory()
    async with factory() as s:
        return (
            await s.execute(text(
                "SELECT id FROM tasks WHERE kind = :k AND payload LIKE :p "
                "ORDER BY created_at DESC LIMIT 1"
            ), {"k": KIND_INGEST_FILE, "p": f'%\"{file_id}\"%'})
        ).scalar_one()


async def _upload_md(client: httpx.AsyncClient, path: str, body: bytes) -> dict:
    r = await client.post(
        "/v1/upload",
        params={"remote_path": path},
        files={"file": (path.split("/")[-1], io.BytesIO(body), "text/markdown")},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def main() -> None:
    _install_fake_llm()
    await _create_schema()

    transport = ASGITransport(app=app)
    runner = TaskRunner()
    factory = get_session_factory()

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # ---- setup: upload one file, run first ingest ----
                _FAKE.responses.append(_payload("first summary", "alpha"))
                up = await _upload_md(c, "/notes/a.md", b"# A\n\ntext\n")
                file_id, entry_id = up["file_id"], up["entry_id"]
                first_task = await _latest_ingest_task_id(file_id)
                assert await _wait_for_task_done(first_task) == "done"

                async with factory() as s:
                    file_row = await s.get(File, file_id)
                    assert file_row.summary == "first summary"
                    assert file_row.ingest_status == "done"
                    tag_names_before = set((await s.execute(
                        select(Tag.name).join(EntryTag, Tag.id == EntryTag.tag_id)
                        .where(EntryTag.entry_id == entry_id)
                    )).scalars().all())
                    assert "alpha" in tag_names_before
                    first_ingested_at = file_row.ingested_at
                print("[1] initial ingest done; summary='first summary', tags include 'alpha'")

                # ---- single-file reprocess ----
                _FAKE.responses.append(_payload("REDONE summary", "beta"))
                r = await c.post(f"/v1/files/{file_id}/reprocess")
                assert r.status_code == 200, r.text
                rb = r.json()
                assert rb["file_id"] == file_id
                assert rb["task_id"]
                assert rb["reused"] is False
                second_task = rb["task_id"]
                assert second_task != first_task

                # state cleared right after the request returns,
                # before the runner picks it up
                async with factory() as s:
                    file_row = await s.get(File, file_id)
                    assert file_row.ingest_status == "pending"
                    # ingested_at cleared so the write-once gate releases
                    assert file_row.ingested_at is None
                    # entry_tags purged before re-ingest
                    n_tags = (await s.execute(
                        select(EntryTag).where(EntryTag.entry_id == entry_id)
                    )).all()
                    assert len(n_tags) == 0, "entry_tags should be cleared"
                print("[2] reprocess request: state cleared, new task enqueued, tags purged")

                assert await _wait_for_task_done(second_task) == "done"

                # ---- post-reprocess: content overwritten ----
                async with factory() as s:
                    file_row = await s.get(File, file_id)
                    assert file_row.summary == "REDONE summary", \
                        f"summary not overwritten: {file_row.summary!r}"
                    assert file_row.ingested_at is not None
                    assert file_row.ingested_at != first_ingested_at
                    tag_names_after = set((await s.execute(
                        select(Tag.name).join(EntryTag, Tag.id == EntryTag.tag_id)
                        .where(EntryTag.entry_id == entry_id)
                    )).scalars().all())
                    assert "beta" in tag_names_after
                    assert "alpha" not in tag_names_after, \
                        "old tag should not be on entry after reprocess"
                print("[3] reprocess produced new summary + new tags; old gone")

                # ---- 404 on missing file ----
                r = await c.post("/v1/files/does-not-exist/reprocess")
                assert r.status_code == 404
                print("[4] missing file → 404")

                # ---- bulk by file_ids ----
                _FAKE.responses.append(_payload("doc2 first", "gamma"))
                up2 = await _upload_md(c, "/notes/b.md", b"# B\n\nmore\n")
                file_id2 = up2["file_id"]
                t2 = await _latest_ingest_task_id(file_id2)
                assert await _wait_for_task_done(t2) == "done"

                _FAKE.responses.append(_payload("REDONE 1", "delta"))
                _FAKE.responses.append(_payload("REDONE 2", "epsilon"))
                r = await c.post(
                    "/v1/files/reprocess",
                    json={"file_ids": [file_id, file_id2]},
                )
                assert r.status_code == 200, r.text
                rb = r.json()
                assert rb["file_count"] == 2
                assert len(rb["task_ids"]) == 2
                assert rb["skipped_count"] == 0
                for tid in rb["task_ids"]:
                    assert await _wait_for_task_done(tid) == "done"

                async with factory() as s:
                    s1 = (await s.get(File, file_id)).summary
                    s2 = (await s.get(File, file_id2)).summary
                    assert s1 == "REDONE 1"
                    assert s2 == "REDONE 2"
                print("[5] bulk file_ids: both files re-ingested with fresh content")

                # ---- bulk all=true skips deleted files ----
                # soft-delete one of the entries so its file becomes
                # "live" only via the other entry (none here) — easier:
                # soft-delete the File row directly via SQL.
                async with factory() as s:
                    await s.execute(
                        text("UPDATE files SET deleted_at = CURRENT_TIMESTAMP "
                             "WHERE id = :id"),
                        {"id": file_id2},
                    )
                    await s.commit()

                _FAKE.responses.append(_payload("REDONE again", "zeta"))
                r = await c.post("/v1/files/reprocess", json={"all": True})
                assert r.status_code == 200, r.text
                rb = r.json()
                # Only the live file (file_id) should be in the count.
                assert rb["file_count"] == 1, f"expected 1 live, got {rb}"
                for tid in rb["task_ids"]:
                    assert await _wait_for_task_done(tid) == "done"
                print("[6] bulk all=true: deleted files excluded")

                # ---- body validation ----
                r = await c.post("/v1/files/reprocess", json={})
                assert r.status_code == 422, r.text
                r = await c.post(
                    "/v1/files/reprocess",
                    json={"all": True, "file_ids": ["x"]},
                )
                assert r.status_code == 422, r.text
                r = await c.post("/v1/files/reprocess", json={"file_ids": []})
                assert r.status_code == 422, r.text
                print("[7] body validation: 0 / 2+ filters / empty file_ids → 422")

        finally:
            await runner.stop()

    print("\nALL REPROCESS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
