"""Unit checks for default full-document OCR ingest."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from marginalia.pipelines import pdf as pdf_module
from marginalia.pipelines.pdf import PdfPipeline


def test_ocr_ingest_default_is_uncapped() -> None:
    original = pdf_module.OCR_MAX_PAGES
    try:
        pdf_module.OCR_MAX_PAGES = None
        assert pdf_module._ocr_configured_page_cap() is None
        assert pdf_module._ocr_pages_to_process(1000) == 1000

        coverage = PdfPipeline._coverage(
            total_pages=1000,
            indexed_pages=1000,
            chunk_count=25,
            text_truncated=False,
            ocr_used=True,
            ocr_pages_done=1000,
            partial_reasons=[],
            max_index_pages=pdf_module._ocr_configured_page_cap(),
        )
        assert coverage["indexed_partial"] is False
        assert coverage["partial_reasons"] == []
        assert "max_index_pages" not in coverage
    finally:
        pdf_module.OCR_MAX_PAGES = original


def test_ocr_pdf_pages_batches_full_uncapped(monkeypatch) -> None:
    render_calls: list[tuple[int, int]] = []

    def fake_render(
        pdf_bytes: bytes,
        page_count: int,
        *,
        start_page: int = 0,
    ) -> list[bytes]:
        assert pdf_bytes == b"pdf"
        render_calls.append((start_page, page_count))
        return [b"jpeg"] * page_count

    def fake_downscale(jpeg_bytes: bytes, *, max_long_edge: int):
        assert jpeg_bytes == b"jpeg"
        return b"scaled", "image/jpeg"

    class FakeVision:
        async def complete(self, request):
            return SimpleNamespace(text="OCR text")

    original_cap = pdf_module.OCR_MAX_PAGES
    original_batch = pdf_module.OCR_RENDER_BATCH_PAGES
    try:
        pdf_module.OCR_MAX_PAGES = None
        pdf_module.OCR_RENDER_BATCH_PAGES = 20
        monkeypatch.setattr(pdf_module, "_render_pdf_pages_to_jpeg", fake_render)
        monkeypatch.setattr(pdf_module, "downscale_for_vlm", fake_downscale)
        monkeypatch.setattr(pdf_module, "get_chat_client", lambda profile: FakeVision())

        out = asyncio.run(pdf_module._ocr_pdf_pages(b"pdf", 45))

        assert len(out) == 45
        assert all(text == "OCR text" for text in out)
        assert render_calls == [(0, 20), (20, 20), (40, 5)]
    finally:
        pdf_module.OCR_MAX_PAGES = original_cap
        pdf_module.OCR_RENDER_BATCH_PAGES = original_batch


def test_ocr_ingest_explicit_cap_still_marks_partial() -> None:
    original = pdf_module.OCR_MAX_PAGES
    try:
        pdf_module.OCR_MAX_PAGES = 5
        assert pdf_module._ocr_configured_page_cap() == 5
        assert pdf_module._ocr_pages_to_process(8) == 5

        coverage = PdfPipeline._coverage(
            total_pages=8,
            indexed_pages=5,
            chunk_count=1,
            text_truncated=False,
            ocr_used=True,
            ocr_pages_done=5,
            partial_reasons=["ocr_page_cap"],
            max_index_pages=pdf_module._ocr_configured_page_cap(),
        )
        assert coverage["indexed_partial"] is True
        assert coverage["partial_reasons"] == ["ocr_page_cap"]
        assert coverage["max_index_pages"] == 5
    finally:
        pdf_module.OCR_MAX_PAGES = original
