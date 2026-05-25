"""End-to-end ingest sanity check.

Run:
    .venv/Scripts/python tests/test_ingest_e2e.py

Verifies:
  1. Upload .md file → ingest_file task is enqueued.
  2. With a stubbed LLM client (no real network), the task runner picks it up.
  3. After the task completes:
     - files.summary / description / extra / kind are written
     - files.ingested_at is locked (write-once)
     - files.ingest_status == 'done'
     - file_entry.catalog_id is set; chain catalogs created
     - file_entry.extra written
     - entry_tags rows added with source='ingest'
     - audit kinds: ingest_status_changed (×3 in: processing/done/maybe more),
       catalog_created, tag_created
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_ingest_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"  # we drive the runner manually below
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select, text

# Force settings cache reset and reset llm cached clients before tests touch them.
from marginalia.config import get_settings  # noqa: E402
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import (  # noqa: E402
    AuditEvent, Base, Catalog, EntryTag, File, FileEntry, Tag,
)
from marginalia.db.models.tasks import Task  # noqa: E402
from marginalia.llm.types import (  # noqa: E402
    ChatRequest, ChatResponse, TokenUsage,
)
from marginalia.main import app  # noqa: E402
from marginalia.tasks.kinds import KIND_INGEST_FILE  # noqa: E402
from marginalia.tasks.runner import TaskRunner  # noqa: E402


# ---- fake LLM client --------------------------------------------------------

CALL_LOG: list[ChatRequest] = []


class _FakeChatClient:
    profile_name = "ingest"
    model = "fake-model"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        # Return a canned, schema-compliant pipeline output.
        payload = {
            "summary": "A short note describing how Marginalia handles ingestion.",
            "description": {
                "sections": [
                    {
                        "id": "s1",
                        "title": "Overview",
                        "anchor": {"unit": "heading", "path": "1"},
                        "summary": "High-level intro to ingestion.",
                        "key_terms": ["ingest", "pipeline", "metadata"],
                    },
                    {
                        "id": "s2",
                        "title": "Pipeline",
                        "anchor": {"unit": "heading", "path": "2"},
                        "summary": "How the text pipeline works.",
                        "key_terms": ["text", "json", "schema"],
                    },
                ],
            },
            "kind": "text",
            "extra": "Themes: indexing, summarization, structured output.",
            "entry_extra": "Sits beside other research notes; references the ingest design.",
            "entry_catalog_path": ["Research", "Marginalia"],
            "entry_tags": [
                {"name": "marginalia", "facet": "topic"},
                {"name": "ingest-pipeline", "facet": "topic"},
                {"name": "markdown", "facet": "form"},
                {"name": "english", "facet": "language"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2000, output_tokens=400, cache_read_tokens=1500),
            parsed_json=payload,
        )


def _install_fake_llm() -> None:
    llm.reset_clients_cache()
    llm.factory.get_chat_client.cache_clear()  # type: ignore[attr-defined]
    fake = _FakeChatClient()
    # Patch the factory so any code requesting a chat client gets our fake.
    def _fake_factory(profile: str = "ingest"):
        return fake
    llm.factory.get_chat_client = _fake_factory  # type: ignore[assignment]
    # text pipeline imports the symbol at module load — patch it there too.
    import marginalia.pipelines.text as text_mod
    text_mod.get_chat_client = _fake_factory  # type: ignore[assignment]


# ---- helpers ---------------------------------------------------------------

async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_task_done(task_id: str, timeout: float = 8.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (
                await s.execute(text(
                    "SELECT status, last_error FROM tasks WHERE id = :id"
                ), {"id": task_id})
            ).first()
            if row is None:
                raise RuntimeError(f"task {task_id} disappeared")
            status, last_error = row
            if status in ("done", "dead"):
                return status
        await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id} did not finish within {timeout}s")


# ---- main ------------------------------------------------------------------

async def main() -> None:
    _install_fake_llm()
    await _create_schema()

    transport = ASGITransport(app=app)
    runner = TaskRunner()

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # 1) upload a markdown doc — auto-creates folders, enqueues ingest
                doc = (
                    "# Overview\n\nMarginalia indexes documents using a small\n"
                    "library of pipelines.\n\n# Pipeline\n\nEach pipeline emits\n"
                    "structured JSON describing the document.\n"
                ).encode("utf-8")
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/research/marginalia/notes.md"},
                    files={"file": ("notes.md", io.BytesIO(doc), "text/markdown")},
                )
                assert r.status_code == 201, r.text
                up = r.json()
                file_id = up["file_id"]
                entry_id = up["entry_id"]
                print("[upload]", up)

            # 2) wait for the ingest_file task to finish
            factory = get_session_factory()
            async with factory() as s:
                task_id = (
                    await s.execute(text(
                        "SELECT id FROM tasks WHERE kind = :k AND payload LIKE :p"
                    ), {"k": KIND_INGEST_FILE, "p": f'%"{file_id}"%'})
                ).scalar_one()
            print("[task] waiting on", task_id)
            status = await _wait_for_task_done(task_id, timeout=10.0)
            print("[task] final status:", status)
            assert status == "done", f"task did not succeed: status={status}"

            # 3) verify the LLM was actually called via our fake
            assert len(CALL_LOG) == 1, f"expected 1 LLM call, got {len(CALL_LOG)}"
            req = CALL_LOG[0]
            assert req.json_schema is not None
            assert "Index the document below" in (
                req.messages[0].content[0].text  # type: ignore[index, attr-defined]
            )
            print("[llm] system prompt len:", len(req.system or ""))
            print("[llm] usage path used cache_breakpoints:", req.cache_breakpoints)

            # 4) DB invariants
            async with factory() as s:
                file_row = await s.get(File, file_id)
                entry_row = await s.get(FileEntry, entry_id)
                assert file_row.ingest_status == "done"
                assert file_row.ingested_at is not None
                assert file_row.summary
                assert file_row.kind == "text"
                assert isinstance(file_row.description, dict)
                assert "sections" in file_row.description
                assert len(file_row.description["sections"]) == 2
                assert file_row.extra and "Themes" in file_row.extra

                assert entry_row.catalog_id is not None
                assert entry_row.extra and "research" in entry_row.extra.lower()

                # catalog chain
                cat = await s.get(Catalog, entry_row.catalog_id)
                assert cat.name == "Marginalia"
                parent = await s.get(Catalog, cat.parent_id)
                assert parent.name == "Research"
                assert parent.parent_id is None

                # tags
                tag_rows = (
                    await s.execute(
                        select(Tag.name, Tag.facet)
                        .join(EntryTag, Tag.id == EntryTag.tag_id)
                        .where(EntryTag.entry_id == entry_id)
                    )
                ).all()
                tag_pairs = {(n, f) for n, f in tag_rows}
                print("[tags]", tag_pairs)
                assert ("marginalia", "topic") in tag_pairs
                assert ("markdown", "form") in tag_pairs
                assert ("english", "language") in tag_pairs

                # entry_tags source
                src = (
                    await s.execute(
                        select(EntryTag.source)
                        .where(EntryTag.entry_id == entry_id)
                        .limit(1)
                    )
                ).scalar_one()
                assert src == "ingest"

                # audit kinds
                kinds = (await s.execute(
                    text("SELECT DISTINCT kind FROM audit_events ORDER BY kind")
                )).scalars().all()
                print("[audit]", kinds)
                for required in (
                    "folder_created", "file_created", "entry_created",
                    "task_enqueued", "ingest_status_changed",
                    "catalog_created", "tag_created",
                ):
                    assert required in kinds, f"missing audit kind: {required}"

            # 5) Idempotence: running the handler again on the same file_id
            #    must NOT re-write content fields (write-once).
            async with factory() as s:
                first_ingested_at = (
                    await s.execute(
                        text("SELECT ingested_at FROM files WHERE id=:id"),
                        {"id": file_id},
                    )
                ).scalar_one()

            from marginalia.tasks.handlers.ingest_file import handle_ingest_file
            await handle_ingest_file({"file_id": file_id})

            async with factory() as s:
                second_ingested_at = (
                    await s.execute(
                        text("SELECT ingested_at FROM files WHERE id=:id"),
                        {"id": file_id},
                    )
                ).scalar_one()
            assert first_ingested_at == second_ingested_at, "ingested_at changed on re-run!"
            assert len(CALL_LOG) == 1, "pipeline ran a second time despite write-once"

        finally:
            await runner.stop()

    print("\nALL INGEST E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
