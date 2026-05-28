from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from marginalia.pipelines.pdf import PdfPipeline
from marginalia.pipelines.pdf_text import (
    extract_pdf_page_labels,
    extract_pdf_text_range,
    resolve_page_label,
)
from marginalia.pipelines.text import TextPipeline


class _FakeStorage:
    def __init__(self, payload: bytes, *, chunk_size: int | None = None):
        self.payload = payload
        self.chunk_size = chunk_size or len(payload)

    async def get(self, key: str):  # noqa: ARG002
        for start in range(0, len(self.payload), self.chunk_size):
            yield self.payload[start:start + self.chunk_size]


def _build_text_pdf(page_count: int) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    for i in range(1, page_count + 1):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.multi_cell(0, 8, text=f"Physical page {i}\nUnique token p{i:03d}")
    return bytes(pdf.output())


def _with_labels(pdf_bytes: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.constants import PageLabelStyle

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.set_page_label(0, 1, style=PageLabelStyle.LOWERCASE_ROMAN, start=1)
    writer.set_page_label(2, len(reader.pages) - 1, style=PageLabelStyle.DECIMAL, start=1)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_page_labels_map_printed_page_to_physical_page() -> None:
    pdf_bytes = _with_labels(_build_text_pdf(8))

    labels = extract_pdf_page_labels(pdf_bytes)
    assert labels[:6] == ["i", "ii", "1", "2", "3", "4"]
    assert resolve_page_label(labels, "4") == 6

    seg = PdfPipeline()._slice(pdf_bytes, {"page_label": "4"})
    assert seg.error is None
    assert "Physical page 6" in seg.text
    assert "Physical page 4" not in seg.text
    assert seg.extras["resolved_page"] == 6
    assert seg.extras["page_label"] == "4"


def test_pdf_page_window_extracts_only_requested_pages() -> None:
    pdf_bytes = _build_text_pdf(30)
    doc = extract_pdf_text_range(pdf_bytes, page_start=25, page_end=25)
    assert doc.total_pages == 30
    assert doc.page_start == 25
    assert len(doc.pages) == 1
    assert "Unique token p025" in doc.pages[0]

    seg = PdfPipeline()._slice(pdf_bytes, {"page_start": 25, "page_end": 25})
    assert seg.error is None
    assert "Unique token p025" in seg.text
    assert "Unique token p001" not in seg.text


def test_pdf_default_read_is_windowed_for_long_documents() -> None:
    pdf_bytes = _build_text_pdf(30)
    seg = PdfPipeline()._slice(pdf_bytes, {})
    assert seg.error is None
    assert "Unique token p001" in seg.text
    assert "Unique token p025" not in seg.text
    assert seg.extras["read_truncated"] is True
    assert seg.extras["next_page_start"] == 21


def test_text_default_read_cap_tracks_requested_window() -> None:
    body = ("alpha\n" * 200_000).encode("utf-8")
    file_row = SimpleNamespace(storage_key="long.txt", size_bytes=len(body))
    seg = asyncio.run(TextPipeline().read_segment(
        file_row=file_row,
        args={"max_chars": 200},
        storage=_FakeStorage(body, chunk_size=1024),
    ))
    assert seg.error is None
    assert len(seg.text) == 200
    assert seg.extras["source_truncated"] is True
    assert seg.extras["source_bytes_read"] < 20_000
