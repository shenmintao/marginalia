"""OCR page-cap boundary — scanned-PDF with > OCR_MAX_PAGES.

Run:
    .venv/Scripts/python tests/test_pdf_ocr_cap_e2e.py

Verifies that a scanned PDF longer than OCR_MAX_PAGES (50 by default):

  - Triggers the OCR path normally (avg chars/page below threshold).
  - OCR is called exactly OCR_MAX_PAGES times, NOT total_pages times.
  - description.ocr.pages_processed reports the cap, not total_pages.
  - description.ocr.pages_total reports the full page count.
  - Ingest status is `done` (never `failed`) — the cap is graceful, not
    an error.
  - Audit trail does not include `failed`.

Important: we patch OCR_MAX_PAGES down to 5 for this test so we don't
need to render 60 pages just to verify the boundary.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_pdf_ocr_cap_e2e_data"
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
from marginalia.pipelines import pdf as pdf_module  # noqa: E402
from marginalia.storage import get_storage  # noqa: E402


# Lower the cap for this test so 8 pages is enough to hit it.
TEST_TOTAL_PAGES = 8
TEST_CAP = 5
ORIGINAL_CAP = pdf_module.OCR_MAX_PAGES
pdf_module.OCR_MAX_PAGES = TEST_CAP


VISION_CALLS: list[ChatRequest] = []
INGEST_CALLS: list[ChatRequest] = []


def _build_long_scanned_pdf() -> bytes:
    """A multi-page PDF with empty text on each page, simulating a long
    scan."""
    from fpdf import FPDF
    pdf = FPDF()
    for _ in range(TEST_TOTAL_PAGES):
        pdf.add_page()
    return bytes(pdf.output())


# ---- fakes -----------------------------------------------------------------

class _FakeVisionOCR:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        idx = len(VISION_CALLS) + 1
        VISION_CALLS.append(request)
        text_md = f"Page {idx} OCR text. Lorem ipsum content body for indexing."
        return ChatResponse(
            text=text_md, tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2200, output_tokens=200),
            parsed_json=None,
        )


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        INGEST_CALLS.append(request)
        tagged = f"""<summary>
Long scanned PDF; OCRed first 5 of 8 pages.
</summary>
<description>
OCR coverage is limited to the configured page cap.
</description>
<sections>
s1 | pages 1-{TEST_CAP} | OCR'd portion | First 5 pages. | lorem, ipsum
</sections>
<extra>
</extra>
<entry_extra>
</entry_extra>
<catalog_path>Tests</catalog_path>
<tags>
form: ocr-cap-boundary
</tags>"""
        return ChatResponse(
            text=tagged, tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=900, output_tokens=200),
            parsed_json=None,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    fake_v = _FakeVisionOCR()
    fake_i = _FakeIngest()

    def _factory(profile: str = "ingest"):
        if profile == "vision":
            return fake_v
        return fake_i

    pdf_module.get_chat_client = _factory  # type: ignore[assignment]


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
    try:
        _install_fakes()
        await _create_schema()

        pdf_bytes = _build_long_scanned_pdf()
        print(f"[setup] {TEST_TOTAL_PAGES}-page scanned PDF: "
              f"{len(pdf_bytes)} bytes; OCR cap = {TEST_CAP}")

        file_id = await _seed_pdf(pdf_bytes, "long_scan.pdf")
        await _ingest(file_id)

        factory = get_session_factory()
        async with factory() as s:
            f = await s.get(File, file_id)
            assert f.ingest_status == "done", \
                f"long-scan with OCR cap should still be 'done', got {f.ingest_status}"
            ocr = (f.description or {}).get("ocr") or {}
            print(f"[1] ingest done; description.ocr = {ocr}")

            assert ocr.get("pages_total") == TEST_TOTAL_PAGES, \
                f"pages_total should reflect full count, got {ocr.get('pages_total')}"
            assert ocr.get("pages_processed") == TEST_CAP, \
                f"pages_processed should equal cap, got {ocr.get('pages_processed')}"
            print(f"[2] description correctly reports "
                  f"{ocr['pages_processed']}/{ocr['pages_total']} pages OCRed")
            coverage = (f.description or {}).get("coverage") or {}
            assert coverage.get("total_pages") == TEST_TOTAL_PAGES, coverage
            assert coverage.get("indexed_pages") == TEST_CAP, coverage
            assert coverage.get("indexed_partial") is True, coverage
            assert "ocr_page_cap" in coverage.get("partial_reasons", []), coverage
            assert coverage.get("max_index_pages") == TEST_CAP, coverage
            print(f"[2b] coverage marks OCR cap explicitly: {coverage}")

        assert len(VISION_CALLS) == TEST_CAP, \
            f"VLM should be called exactly {TEST_CAP} times, "\
            f"got {len(VISION_CALLS)}"
        print(f"[3] VLM called exactly {len(VISION_CALLS)} times "
              f"(NOT {TEST_TOTAL_PAGES})")

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
        assert "failed" not in statuses, \
            f"OCR-cap path should never set failed: {statuses}"
        print(f"[4] audit trail: {statuses}")

        print("\nALL PDF_OCR_CAP E2E CHECKS PASSED")
    finally:
        pdf_module.OCR_MAX_PAGES = ORIGINAL_CAP


if __name__ == "__main__":
    asyncio.run(_main())
