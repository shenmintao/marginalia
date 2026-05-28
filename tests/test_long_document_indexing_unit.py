from __future__ import annotations

import pytest

from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.pipelines.base import PipelineContext
from marginalia.pipelines.pdf import PdfPipeline
from marginalia.pipelines.text import TextPipeline
from marginalia.pipelines import text as text_mod


class _BytesStorage:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def get(self, key: str):
        yield self.body


def _ctx(*, size: int = 100) -> PipelineContext:
    return PipelineContext(
        file_id="f1",
        storage_key="k1",
        sha256="a" * 64,
        size_bytes=size,
        mime_type="application/pdf",
        original_ext=".pdf",
        folder_path="/tests",
        sibling_names=[],
        display_name="long.pdf",
        catalog_sketch=[],
        tag_vocabulary=[],
    )


def _tagged(
    *,
    summary: str,
    sections: str = "",
    description: str = "",
    extra: str = "",
    tags: str = "topic: long-document\nform: pdf\nlanguage: en",
) -> str:
    return f"""<summary>
{summary}
</summary>
<description>
{description}
</description>
<sections>
{sections}
</sections>
<extra>
{extra}
</extra>
<entry_extra>
test entry extra
</entry_extra>
<catalog_path>Tests / Long Documents</catalog_path>
<tags>
{tags}
</tags>"""


def test_pdf_read_segment_can_access_pages_past_ingest_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    def fake_extract(pdf_bytes: bytes, *, max_pages: int | None = 60) -> list[str]:
        calls.append(max_pages)
        return [f"text page {i}" for i in range(1, 101)]

    monkeypatch.setattr(PdfPipeline, "_extract_text", staticmethod(fake_extract))

    seg = PdfPipeline()._slice(b"fake", {"page_start": 90, "page_end": 91})

    assert seg.error is None
    assert calls == [None]
    assert "text page 90" in seg.text
    assert "text page 91" in seg.text
    assert seg.extras["total_pages"] == 100


@pytest.mark.asyncio
async def test_pdf_long_ingest_chunks_then_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import marginalia.pipelines.pdf as pdf_mod

    def fake_extract(pdf_bytes: bytes, *, max_pages: int | None = 60) -> list[str]:
        return [
            (
                f"Page {i} discusses topic {i}. "
                "This page has enough extracted text to be treated as a "
                "normal text-layer PDF rather than scanned OCR input."
            )
            for i in range(1, 66)
        ]

    monkeypatch.setattr(PdfPipeline, "_extract_text", staticmethod(fake_extract))
    monkeypatch.setattr(pdf_mod, "has_vision_profile", lambda: False)

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: ChatRequest) -> ChatResponse:
            self.calls += 1
            if "aggregate" in (request.system or "").lower():
                text = _tagged(
                    summary="A long PDF covering all indexed page ranges.",
                    description="Aggregate description from section summaries.",
                    extra="notable_terms: topic 1; topic 65",
                )
            elif self.calls == 1:
                text = _tagged(
                    summary="First page range.",
                    sections="s1 | 1-40 | First range | Covers early pages. | topic 1, topic 40",
                )
            else:
                text = _tagged(
                    summary="Second page range.",
                    sections="s1 | 41-65 | Second range | Covers late pages. | topic 65",
                )
            return ChatResponse(
                text=text,
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    fake = FakeClient()
    monkeypatch.setattr(pdf_mod, "get_chat_client", lambda profile="ingest": fake)

    result = await PdfPipeline().run(ctx=_ctx(), storage=_BytesStorage(b"pdf"))

    coverage = result.description["coverage"]
    assert coverage["chunked"] is True
    assert coverage["total_pages"] == 65
    assert coverage["indexed_pages"] == 65
    assert coverage["indexed_partial"] is False
    assert len(result.description["sections"]) == 2
    assert result.description["sections"][1]["anchor"]["value"] == "41-65"
    assert "topic 65" in (result.extra or "")
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_text_long_ingest_chunks_then_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    body = "\n".join(f"line {i} keyword-{i}" for i in range(1, 9000)).encode()

    class FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: ChatRequest) -> ChatResponse:
            self.calls += 1
            if "aggregate" in (request.system or "").lower():
                text = _tagged(
                    summary="A long text file indexed from line-range sections.",
                    description="Aggregate description from line-range summaries.",
                    extra="notable_terms: keyword-1; keyword-8999",
                    tags="topic: long-text\nform: markdown\nlanguage: en",
                )
            else:
                idx = self.calls
                start = 1 if idx == 1 else (idx - 1) * 2500
                end = idx * 2500
                text = _tagged(
                    summary=f"Line range {idx}.",
                    sections=(
                        f"s1 | {start}-{end} | Lines {start}-{end} | "
                        f"Covers range {idx}. | keyword-{start}, keyword-{end}"
                    ),
                    tags="topic: long-text\nform: markdown\nlanguage: en",
                )
            return ChatResponse(
                text=text,
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(),
            )

    fake = FakeClient()
    monkeypatch.setattr(text_mod, "get_chat_client", lambda profile="ingest": fake)

    ctx = _ctx(size=len(body))
    ctx.mime_type = "text/markdown"
    ctx.original_ext = ".md"
    ctx.display_name = "long.md"
    result = await TextPipeline().run(ctx=ctx, storage=_BytesStorage(body))

    coverage = result.description["coverage"]
    assert coverage["chunked"] is True
    assert coverage["indexed_partial"] is False
    assert len(result.description["sections"]) >= 2
    assert result.description["sections"][0]["anchor"]["unit"] == "lines"
    assert "keyword-8999" in (result.extra or "")
    assert fake.calls >= 3


def test_text_read_segment_cap_expands_for_late_offsets_and_deep_reads() -> None:
    class Row:
        size_bytes = 256 * 1024 * 1024

    late_offset_cap = text_mod._read_cap_for_args(
        {"offset": 50_000_000, "max_chars": 1000},
        file_row=Row(),
    )

    assert late_offset_cap >= (50_000_000 + 1000 + 4096) * 4
    assert text_mod._read_cap_for_args(
        {"line_start": 2_000_000},
        file_row=Row(),
    ) == text_mod.READ_SEGMENT_DEEP_BYTES_CAP
