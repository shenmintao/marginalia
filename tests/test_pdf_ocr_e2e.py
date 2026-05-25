"""Scanned-PDF OCR via VLM — end-to-end happy path.

Run:
    .venv/Scripts/python tests/test_pdf_ocr_e2e.py

Companion to test_pdf_pipeline_e2e.py [4]: that test verifies a truly
empty scanned PDF still ends up in 'failed' (vlm returned nothing). This
test verifies the *success* path:

  1. Upload a PDF whose text layer is empty (so the pipeline triggers
     the OCR fallback).
  2. Mock the vision client to return realistic OCR markdown for the
     page.
  3. Mock the ingest client to return a structured PDF index.
  4. Assert ingest succeeds, description carries description.ocr.engine
     == 'vlm', the ingest LLM saw a prompt containing the OCR'd text,
     and the audit trail shows the doc going through processing → done.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_pdf_ocr_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import text  # noqa: E402

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File  # noqa: E402
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.storage import get_storage  # noqa: E402


VISION_CALLS: list[ChatRequest] = []
INGEST_CALLS: list[ChatRequest] = []

OCR_PAGE_MD = (
    "# Raft Consensus\n"
    "## 1. Background\n"
    "Raft is a consensus algorithm designed to be more understandable than "
    "Paxos. Servers agree on a sequence of log entries.\n"
    "## 2. Leader Election\n"
    "When a leader fails, followers timeout and become candidates. They "
    "request votes from peers.\n"
)


def _build_scanned_pdf() -> bytes:
    """A 1-page PDF with no text layer (empty page) — pdfium's text
    extractor returns ~0 chars, so the OCR fallback triggers."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    return bytes(pdf.output())


# ---- fakes -----------------------------------------------------------------

class _FakeVisionOCR:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        VISION_CALLS.append(request)
        return ChatResponse(
            text=OCR_PAGE_MD, tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2200, output_tokens=300),
            parsed_json=None,
        )


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        INGEST_CALLS.append(request)
        payload = {
            "summary": "A short scanned PDF on Raft consensus.",
            "description": {
                "sections": [
                    {
                        "id": "s1", "title": "Background",
                        "anchor": {"unit": "pages", "value": "1-1"},
                        "summary": "Raft motivation.",
                        "key_terms": ["raft", "consensus"],
                    },
                ],
            },
            "kind": "text",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Research", "Consensus"],
            "entry_tags": [
                {"name": "raft", "facet": "topic"},
                {"name": "scanned-pdf", "facet": "form"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload), tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=900, output_tokens=200),
            parsed_json=payload,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    fake_vision = _FakeVisionOCR()
    fake_ingest = _FakeIngest()

    def _factory(profile: str = "ingest"):
        if profile == "vision":
            return fake_vision
        return fake_ingest

    import marginalia.pipelines.pdf as pmod
    pmod.get_chat_client = _factory  # type: ignore[assignment]


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_pdf(body: bytes, name: str) -> str:
    from marginalia.services.upload import upload
    storage = get_storage()

    async def _stream():
        yield body

    factory = get_session_factory()
    async with factory() as db:
        result = await upload(
            db, storage,
            stream=_stream(), fallback_name=name,
            remote_path=f"/tests/{name}",
            content_type="application/pdf",
        )
        await db.commit()
        return result.file_id


async def _ingest(file_id: str) -> None:
    from marginalia.tasks.handlers.ingest_file import handle_ingest_file
    await handle_ingest_file({"file_id": file_id, "entry_id": None})


# ---- test ------------------------------------------------------------------

async def _main() -> None:
    _install_fakes()
    await _create_schema()

    pdf_bytes = _build_scanned_pdf()
    print("[setup] scanned PDF:", len(pdf_bytes), "bytes")

    file_id = await _seed_pdf(pdf_bytes, "scanned.pdf")
    print(f"[1] uploaded scanned PDF, file_id = {file_id[:8]}")

    await _ingest(file_id)
    print(f"[2] ingest finished; vision_calls={len(VISION_CALLS)}, "
          f"ingest_calls={len(INGEST_CALLS)}")

    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.ingest_status == "done", \
            f"expected ingest done, got {f.ingest_status}"
        assert "ocr" in (f.description or {}), \
            f"description should carry ocr metadata; keys={list((f.description or {}).keys())}"
        ocr = f.description["ocr"]
        assert ocr["engine"] == "vlm", f"engine={ocr.get('engine')}"
        assert ocr["pages_processed"] == 1, f"pages_processed={ocr}"
        print(f"[3] description.ocr = {ocr}")

    assert len(VISION_CALLS) == 1, \
        f"expected 1 vision call (1 page), got {len(VISION_CALLS)}"
    print("[4] vision was called exactly once (for the single page)")

    assert len(INGEST_CALLS) == 1, \
        f"expected 1 ingest call, got {len(INGEST_CALLS)}"
    ingest_prompt = ""
    for content in INGEST_CALLS[0].messages[0].content:
        if hasattr(content, "text"):
            ingest_prompt += content.text
    assert "Leader Election" in ingest_prompt or "Raft" in ingest_prompt, \
        "ingest prompt did not include OCR text"
    print("[5] OCR text propagated into ingest prompt")

    async with factory() as s:
        rows = (await s.execute(text(
            "SELECT payload FROM audit_events "
            "WHERE kind='ingest_status_changed' "
            "ORDER BY occurred_at ASC"
        ))).scalars().all()
    statuses = [
        json.loads(p)["status"] if isinstance(p, str) else p["status"]
        for p in rows
    ]
    assert statuses[-1] == "done", f"final audit status: {statuses}"
    assert "failed" not in statuses, \
        f"OCR success path should never set failed: {statuses}"
    print(f"[6] audit trail: {statuses}")

    print("\nALL PDF_OCR E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
