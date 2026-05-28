"""PDF pipeline (DESIGN.md §11.3).

Handles application/pdf and `.pdf`. Strategy: pypdf extracts the text
layer page by page; significant images are concurrently described by
the vision LLM and inlined as `[Figure N.M] ...` lines next to their
pages; the assembled body then goes through the same tagged-response
indexing prompt as the text pipeline, but with page anchors in
`<sections>`.

PDFs without a text layer (scanned images) run the VLM OCR fallback.
The extracted OCR text is indexed like a PDF and stored as page/block
text so read_segment can serve pattern/page/section reads without
another vision call.

read_segment supports page_start / page_end ranges, regex pattern
search across pages, and the generic offset/max_chars chunking.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from marginalia.config import has_vision_profile
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    get_chat_client,
)
from marginalia.llm.tagged_response import (
    render_format_hint,
    render_sections_hint,
)
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    SegmentResult,
)
from marginalia.pipelines._long_index import (
    build_retrieval_extra,
    fallback_section,
    llm_ingest_concurrency,
    parse_index_response,
    render_sections_digest,
    renumber_sections,
)
from marginalia.pipelines.image import downscale_for_vlm
from marginalia.pipelines.pdf_text import (
    extract_pdf_page_labels,
    extract_pdf_text_range,
    pdf_page_count,
    render_pdf_text_pages,
    resolve_page_label,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_PAGES = 60                    # legacy single-prompt page cap
MAX_TOTAL_TEXT_BYTES = 80_000     # ≈ 25-30k tokens cap
PDF_CHUNK_PAGES = 40              # long-doc page window for per-chunk indexing
PDF_TEXT_MAX_INDEX_PAGES = 400    # hard text-layer ingest budget
PDF_SECTION_DIGEST_BYTES = 60_000 # cap the aggregate summary prompt
MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER = 50  # if every page yields fewer chars,
                                       # the doc is probably scanned
OCR_MAX_PAGES = 50                # cap how many pages we OCR per doc
PDF_READ_MAX_PAGES_PER_CALL = 50
PDF_PATTERN_UNSCOPED_MAX_PAGES = 200
PDF_DEFAULT_READ_PAGES = 20
OCR_STORED_TEXT_SAMPLE_CHARS = 20_000
OCR_BLOCK_MAX_CHARS = 8_000
OCR_RENDER_DPI = 200              # JPEG render DPI before VLM (sweet spot)
OCR_VLM_MAX_LONG_EDGE = 2048      # OCR is glyph-sensitive — keep more
                                  # detail than the caption path's 1568


PDF_OCR_PROMPT = """You are an OCR assistant. Extract all body text from the provided document image and output pure Markdown in the document's own language.

Rules:
1. Ignore page headers, footers, and page numbers.
2. Preserve paragraph and heading hierarchy where visible.
3. Use Markdown table syntax for tables.
4. Use LaTeX for math (wrapped with $ or $$).
5. Output ONLY the extracted text. No HTML, no preamble, no commentary.
6. If the page has no recognisable text content, reply only with: No text content."""


PDF_PIPELINE_SYSTEM = """You are Marginalia's PDF document indexer.

You receive the indexed text of a PDF, page-by-page. It may be only the
first `indexed_pages` of a longer PDF; use only the pages provided and do
not infer content from missing pages. Produce a structured index that lets a
downstream agent decide whether to retrieve the document and find the
relevant page.

`summary` is one or two sentences (<=60 Chinese characters / <=30 English words) in the
document's own language — the spine of what the document is and why a
reader would open it. Keep it tight; depth belongs in `description`.
`description` is a free-text walk-through of the document's structure and
key points. `sections` lists every meaningful section/heading; each line
takes the form `id | <pages X-Y> | title | one-or-two-sentence summary |
term1, term2, term3`. Pages are 1-indexed and inclusive. `extra` carries
cross-cutting machine-readable insights as `key: value` lines (one per
line; leave the block empty if nothing notable). `entry_extra` is the
same shape but for position-aware insights. `entry_catalog_path` is a
best-guess classification path. `tags` are 3-10 facet:name pairs; valid
facets are topic | form | time | source | language | extra.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="pages", anchor_example="pages 4-7",
)


PDF_CHUNK_SYSTEM = """You are Marginalia's PDF section indexer.

You receive one page range from a larger PDF. Produce a local index for this
range only. Use the original page numbers shown in the `### Page N` markers.

`summary` briefly states what this range covers. `description` can add a
short walk-through. `sections` is required and should cover every meaningful
heading or logical chunk in the provided range. Keep key terms useful for
later retrieval.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="pages", anchor_example="pages 401-425",
)


PDF_AGGREGATE_SYSTEM = """You are Marginalia's PDF aggregate indexer.

You receive a precomputed section map for the indexed portion of a PDF. Do NOT
read or invent outside that map. If `coverage.indexed_partial` is true, make
the limited coverage clear and do not imply that later pages were reviewed.
Produce only file-level fields: summary, description, extra, entry_extra,
catalog_path, and tags. Do not output a sections block; the caller will
preserve the section map separately in `description.sections`.

Make `extra` retrieval-friendly: include important alternate names, recurring
technical terms, and high-value page ranges from the section map.

""" + render_format_hint()


# Schema kept for legacy callers but no longer fed to the LLM.
PDF_PIPELINE_SCHEMA: dict[str, Any] = {}


class PdfNeedsOcrError(Exception):
    """Raised when the OCR fallback itself failed (e.g. VLM unavailable
    or returned only empty pages). Kept for the dispatcher to mark the
    file as 'failed' with reason 'needs_ocr' so the user can retry once
    the VLM is back up. The text-layer-missing case no longer raises —
    it triggers the OCR path automatically."""

    def __init__(self, *, total_pages: int, total_chars: int) -> None:
        super().__init__(
            f"PDF has no usable text layer "
            f"(pages={total_pages}, chars={total_chars}); needs OCR."
        )
        self.total_pages = total_pages
        self.total_chars = total_chars


_NO_TEXT_LAYER_ERROR = (
    "PDF has no usable text layer; pages may be scanned images. "
    "Pass the `question` parameter to read it with the vision model."
)


@register_pipeline(
    mimes=("application/pdf",),
    exts=(".pdf",),
)
class PdfPipeline(Pipeline):
    name = "pdf"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        body = await self._read_bytes(storage, ctx.storage_key)
        total_pages = self._page_count(body)
        text_index_pages = min(total_pages, PDF_TEXT_MAX_INDEX_PAGES)
        text_per_page = self._extract_text(body, max_pages=text_index_pages)

        vlm_available = has_vision_profile()

        total_chars = sum(len(t) for t in text_per_page)
        ocr_used = False
        ocr_pages_done = 0
        ocr_document_type: str | None = None
        ocr_pages_for_storage: list[str] | None = None
        indexed_pages = len(text_per_page)
        partial_reasons: list[str] = []
        if indexed_pages < total_pages:
            partial_reasons.append("text_page_cap")
        avg_chars = total_chars / max(indexed_pages, 1)
        if total_pages > 0 and avg_chars < MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER:
            if not vlm_available:
                # No VLM profile configured — can't OCR. Mark file as needing
                # OCR so the user can retry once a vision model is wired up.
                raise PdfNeedsOcrError(
                    total_pages=total_pages, total_chars=total_chars,
                )
            log.info(
                "pdf %s appears scanned (pages=%d, avg_chars=%.1f); "
                "running VLM OCR fallback",
                ctx.storage_key, total_pages,
                avg_chars,
            )
            ocr_used = True
            ocr_text_per_page = await _ocr_pdf_pages(body, total_pages)
            ocr_pages_done = sum(1 for t in ocr_text_per_page if t.strip())
            if ocr_pages_done == 0:
                raise PdfNeedsOcrError(
                    total_pages=total_pages, total_chars=total_chars,
                )
            text_per_page = ocr_text_per_page
            total_chars = sum(len(t) for t in text_per_page)
            # OCR is still capped because it bills one VLM call per page.
            indexed_pages = min(total_pages, OCR_MAX_PAGES)
            ocr_pages_for_storage = text_per_page[:indexed_pages]
            ocr_document_type = _classify_ocr_document(
                ocr_pages_for_storage, total_pages=total_pages,
            )
            partial_reasons = []
            if indexed_pages < total_pages:
                partial_reasons.append("ocr_page_cap")

        # Extract embedded figures and describe them via vision profile.
        # Single-image failures degrade to placeholder text; the ingest
        # call below still gets useful context.
        # Skip figure extraction in OCR mode — the page render IS the figure,
        # and we already have its OCR text.
        # Skip entirely when no vision profile is configured: the figures
        # would just produce "(figure description unavailable)" rows.
        if ocr_used or not vlm_available:
            described = []
        else:
            images = extract_images(body, max_pages=indexed_pages)
            described = await describe_images(images) if images else []

        if self._needs_chunked_index(text_per_page[:indexed_pages], described):
            return await self._run_chunked_index(
                ctx=ctx,
                text_per_page=text_per_page[:indexed_pages],
                described=described,
                total_pages=total_pages,
                indexed_pages=indexed_pages,
                ocr_used=ocr_used,
                ocr_pages_done=ocr_pages_done,
                partial_reasons=partial_reasons,
                ocr_pages=ocr_pages_for_storage,
                ocr_document_type=ocr_document_type,
            )

        return await self._run_single_index(
            ctx=ctx,
            text_per_page=text_per_page[:indexed_pages],
            described=described,
            total_pages=total_pages,
            indexed_pages=indexed_pages,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            partial_reasons=partial_reasons,
            ocr_pages=ocr_pages_for_storage,
            ocr_document_type=ocr_document_type,
        )

    @staticmethod
    async def _read_bytes(
        storage: StorageBackend, key: str,
    ) -> bytes:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _page_count(pdf_bytes: bytes) -> int:
        return pdf_page_count(pdf_bytes)

    def _needs_chunked_index(
        self,
        text_per_page: list[str],
        described: list["DescribedImage"],
    ) -> bool:
        if len(text_per_page) > MAX_PAGES:
            return True
        rendered = render_pages_with_figures(text_per_page, described)
        return len(rendered) > MAX_TOTAL_TEXT_BYTES

    async def _run_single_index(
        self,
        *,
        ctx: PipelineContext,
        text_per_page: list[str],
        described: list["DescribedImage"],
        total_pages: int,
        indexed_pages: int,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
    ) -> PipelineResult:
        body_text_raw = render_pages_with_figures(text_per_page, described)
        body_text = self._truncate(body_text_raw)
        text_truncated = len(body_text_raw) > MAX_TOTAL_TEXT_BYTES
        coverage = self._coverage(
            total_pages=total_pages,
            indexed_pages=indexed_pages,
            chunk_count=1,
            text_truncated=text_truncated,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            partial_reasons=partial_reasons,
            max_index_pages=(
                OCR_MAX_PAGES if ocr_used else PDF_TEXT_MAX_INDEX_PAGES
            ),
        )
        user_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "page_count": total_pages,
            "indexed_pages": indexed_pages,
            "figure_count": len(described),
            "ocr_used": ocr_used,
            "ocr_pages_done": ocr_pages_done if ocr_used else 0,
            "ocr_document_type": ocr_document_type if ocr_used else None,
        }
        stable_prefix = (
            "Index the PDF pages below. Hints are advisory; the provided "
            "text and figure captions take precedence. If indexed_pages is "
            "less than page_count, cover only the provided pages and do not "
            "infer missing pages.\n\n"
            + render_format_hint() + "\n"
            + render_sections_hint(anchor_unit="pages", anchor_example="pages 4-7")
        )
        file_content = (
            f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body_text}\n</document>"
        )

        client = get_chat_client("ingest")
        max_out = min(8192, max(2048, len(body_text) // 8))
        resp = await client.complete(ChatRequest(
            system=PDF_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[
                TextBlock(text=stable_prefix),
                TextBlock(text=file_content),
            ])],
            max_tokens=max_out,
            temperature=0.2,
            cache_breakpoints=[0],
        ))
        fields = parse_index_response(resp, anchor_unit="pages")
        if not fields.summary:
            log.warning(
                "pdf pipeline: no <summary> in response. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("pdf pipeline produced empty summary")
        sections = fields.sections or [
            fallback_section(
                title=f"Pages 1-{max(indexed_pages, 1)}",
                anchor_unit="pages",
                anchor_value=f"1-{max(indexed_pages, 1)}",
                summary=fields.summary,
            )
        ]
        return self._result_from_fields(
            fields=fields,
            sections=renumber_sections(sections),
            coverage=coverage,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            ocr_pages=ocr_pages,
            ocr_document_type=ocr_document_type,
        )

    async def _run_chunked_index(
        self,
        *,
        ctx: PipelineContext,
        text_per_page: list[str],
        described: list["DescribedImage"],
        total_pages: int,
        indexed_pages: int,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
    ) -> PipelineResult:
        client = get_chat_client("ingest")
        all_sections: list[dict[str, Any]] = []
        chunk_summaries: list[dict[str, Any]] = []
        truncated_chunks = 0

        chunks = list(enumerate(
            self._iter_prompt_chunks(text_per_page, described),
            start=1,
        ))
        sem = asyncio.Semaphore(llm_ingest_concurrency())

        async def _index_chunk(
            chunk_no: int,
            start: int,
            end: int,
            rendered: str,
            text_truncated: bool,
        ) -> dict[str, Any]:
            async with sem:
                user_payload = {
                    "folder_path": ctx.folder_path,
                    "sibling_names": ctx.sibling_names,
                    "catalog_sketch": ctx.catalog_sketch,
                    "tag_vocabulary": ctx.tag_vocabulary,
                    "page_count": total_pages,
                    "page_start": start,
                    "page_end": end,
                    "chunk_no": chunk_no,
                    "ocr_used": ocr_used,
                    "ocr_document_type": ocr_document_type if ocr_used else None,
                }
                stable_prefix = (
                    "Index this page range from a larger PDF. Use original page "
                    "numbers from the page markers.\n\n"
                    + render_format_hint() + "\n"
                    + render_sections_hint(
                        anchor_unit="pages",
                        anchor_example=f"pages {start}-{end}",
                    )
                )
                file_content = (
                    f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
                    f"<document>\n{rendered}\n</document>"
                )
                resp = await client.complete(ChatRequest(
                    system=PDF_CHUNK_SYSTEM,
                    messages=[ChatMessage(role="user", content=[
                        TextBlock(text=stable_prefix),
                        TextBlock(text=file_content),
                    ])],
                    max_tokens=min(8192, max(2048, len(rendered) // 8)),
                    temperature=0.2,
                    cache_breakpoints=[0],
                ))
            fields = parse_index_response(resp, anchor_unit="pages")
            summary = fields.summary or fields.description_text or f"Pages {start}-{end}"
            sections = fields.sections or [
                fallback_section(
                    title=f"Pages {start}-{end}",
                    anchor_unit="pages",
                    anchor_value=f"{start}-{end}",
                    summary=summary,
                )
            ]
            return {
                "sections": sections,
                "text_truncated": text_truncated,
                "summary": {
                    "page_start": start,
                    "page_end": end,
                    "summary": summary,
                    "description": fields.description_text or "",
                },
            }

        chunk_results = await asyncio.gather(*(
            _index_chunk(chunk_no, start, end, rendered, text_truncated)
            for chunk_no, (start, end, rendered, text_truncated) in chunks
        ))
        for result in chunk_results:
            if result["text_truncated"]:
                truncated_chunks += 1
            all_sections.extend(result["sections"])
            chunk_summaries.append(result["summary"])

        sections = renumber_sections(all_sections)
        coverage = self._coverage(
            total_pages=total_pages,
            indexed_pages=indexed_pages,
            chunk_count=len(chunk_summaries),
            text_truncated=truncated_chunks > 0,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            partial_reasons=partial_reasons,
            max_index_pages=(
                OCR_MAX_PAGES if ocr_used else PDF_TEXT_MAX_INDEX_PAGES
            ),
        )
        if truncated_chunks:
            coverage["truncated_chunks"] = truncated_chunks

        digest = render_sections_digest(
            sections, max_chars=PDF_SECTION_DIGEST_BYTES,
        )
        aggregate_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "coverage": coverage,
            "chunk_summaries": chunk_summaries,
            "ocr_document_type": ocr_document_type if ocr_used else None,
        }
        aggregate_content = (
            f"<context>\n{json.dumps(aggregate_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<section_map>\n{digest}\n</section_map>"
        )
        resp = await client.complete(ChatRequest(
            system=PDF_AGGREGATE_SYSTEM,
            messages=[ChatMessage(role="user", content=[
                TextBlock(text=(
                    "Summarize the indexed PDF coverage from this section map. "
                    "The caller already has `description.sections`; "
                    "produce file-level recall fields only."
                )),
                TextBlock(text=aggregate_content),
            ])],
            max_tokens=8192,
            temperature=0.2,
            cache_breakpoints=[0],
        ))
        fields = parse_index_response(resp, anchor_unit="pages")
        if not fields.summary:
            first = chunk_summaries[0]["summary"] if chunk_summaries else "PDF"
            fields.summary = (
                f"Long PDF indexed into {len(chunk_summaries)} page ranges. "
                f"First range: {first}"
            )
        return self._result_from_fields(
            fields=fields,
            sections=sections,
            coverage=coverage,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            ocr_pages=ocr_pages,
            ocr_document_type=ocr_document_type,
        )

    def _iter_prompt_chunks(
        self,
        text_per_page: list[str],
        described: list["DescribedImage"],
    ):
        start = 0
        n_pages = len(text_per_page)
        while start < n_pages:
            end = min(start + PDF_CHUNK_PAGES, n_pages)
            rendered = render_pages_with_figures(
                text_per_page[start:end],
                described,
                start_page=start + 1,
            )
            while len(rendered) > MAX_TOTAL_TEXT_BYTES and end - start > 1:
                end = start + max(1, (end - start) // 2)
                rendered = render_pages_with_figures(
                    text_per_page[start:end],
                    described,
                    start_page=start + 1,
                )
            text_truncated = False
            if len(rendered) > MAX_TOTAL_TEXT_BYTES:
                rendered = self._truncate(rendered)
                text_truncated = True
            yield start + 1, end, rendered, text_truncated
            start = end

    def _result_from_fields(
        self,
        *,
        fields,
        sections: list[dict[str, Any]],
        coverage: dict[str, Any],
        ocr_used: bool,
        ocr_pages_done: int,
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
    ) -> PipelineResult:
        description: dict[str, Any] = {
            "sections": sections,
            "coverage": coverage,
        }
        if fields.description_text:
            description["text"] = fields.description_text
        if ocr_used:
            stored_pages, block_count = _build_ocr_pages_payload(ocr_pages or [])
            description["ocr"] = {
                "engine": "vlm",
                "pages_total": coverage.get("total_pages"),
                "pages_processed": ocr_pages_done,
                "document_type": ocr_document_type or "document",
                "stored_pages": len(stored_pages),
                "block_count": block_count,
            }
            description["ocr_pages"] = stored_pages
        base_extra = fields.extra
        if ocr_used:
            base_extra = _ocr_retrieval_extra(
                base_extra=fields.extra,
                ocr_pages=ocr_pages or [],
                document_type=ocr_document_type or "document",
            )
        return PipelineResult(
            summary=fields.summary,
            description=description,
            kind="text",
            extra=build_retrieval_extra(
                sections=sections,
                coverage=coverage,
                base_extra=base_extra,
            ),
            entry_extra=fields.entry_extra,
            entry_catalog_path=fields.catalog_path,
            entry_tags=fields.tags,
        )

    @staticmethod
    def _coverage(
        *,
        total_pages: int,
        indexed_pages: int,
        chunk_count: int,
        text_truncated: bool,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        max_index_pages: int,
    ) -> dict[str, Any]:
        reasons = list(dict.fromkeys(partial_reasons))
        if text_truncated and "prompt_text_cap" not in reasons:
            reasons.append("prompt_text_cap")
        indexed_partial = indexed_pages < total_pages or text_truncated
        coverage: dict[str, Any] = {
            "unit": "pages",
            "total_pages": total_pages,
            "indexed_pages": indexed_pages,
            "indexed_partial": indexed_partial,
            "partial_reasons": reasons if indexed_partial else [],
            "max_index_pages": max_index_pages,
            "chunked": chunk_count > 1,
            "chunk_count": chunk_count,
            "text_truncated": text_truncated,
        }
        if ocr_used:
            coverage["ocr_used"] = True
            coverage["ocr_pages_done"] = ocr_pages_done
        return coverage

    # ---- read_segment -----------------------------------------------------

    READ_DEFAULT_MAX_CHARS = 8000

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Two paths, picked by whether this PDF was OCR-indexed at
        ingest and whether the agent passed `question`:

        * description.ocr present + question set → render the requested
          pages to JPEG and ask the VLM the question directly. The
          ingest-time OCR text was lossy by definition; for an actual
          query, sending pixels to the VLM is closer to what was
          originally on the page.
        * description.ocr present + no question: read
          from stored OCR page/block text captured at ingest.
        * otherwise (text-layer PDFs) → existing behaviour: pypdf text
          extraction + page/pattern slicing.
        """
        is_ocr_pdf = _file_was_ocr_indexed(file_row)
        question = (args.get("question") or "").strip() if isinstance(args, dict) else ""
        if is_ocr_pdf:
            if not question:
                return self._slice_ocr_text(file_row, args)
            return await self._answer_with_vlm(
                file_row=file_row, question=question, args=args, storage=storage,
            )
        pdf_bytes = await self._read_bytes(storage, file_row.storage_key)
        return self._slice(pdf_bytes, args)

    async def _answer_with_vlm(
        self,
        *,
        file_row: Any,
        question: str,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Render the requested page range to JPEGs and ask the VLM."""
        if not has_vision_profile():
            return SegmentResult(error=(
                "OCR PDF read with `question` requires the `vision` LLM "
                "profile; configure it before retrying"
            ), extras={"kind": "pdf", "ocr_indexed": True})
        try:
            pdf_bytes = await self._read_bytes(storage, file_row.storage_key)
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF read failed: {exc}",
                                 extras={"kind": "pdf"})

        # Page selection: explicit page_start/page_end if given, else
        # OCR_MAX_PAGES from the start (matches ingest-time coverage).
        try:
            from pypdf import PdfReader
            total_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
        except Exception:  # noqa: BLE001
            total_pages = 0
        ps_arg = args.get("page_start")
        pe_arg = args.get("page_end")
        if ps_arg:
            try:
                ps = max(1, int(ps_arg))
                pe = int(pe_arg) if pe_arg else ps
                pe = max(ps, pe)
            except (TypeError, ValueError):
                return SegmentResult(error="page_start/page_end must be integers")
        else:
            ps, pe = 1, min(total_pages or OCR_MAX_PAGES, OCR_MAX_PAGES)
        # Cap span at OCR_MAX_PAGES to keep the VLM call bounded.
        pe = min(pe, ps + OCR_MAX_PAGES - 1)

        # Render pages [1..pe], then drop everything before ps. The
        # underlying renderer takes a leading page_count, so we render
        # up to pe and slice — the cost difference vs adding a start
        # offset to the helper isn't worth a signature change here.
        try:
            jpegs_all = await asyncio.to_thread(
                _render_pdf_pages_to_jpeg, pdf_bytes, pe,
            )
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF render failed: {exc}",
                                 extras={"kind": "pdf"})
        jpegs = jpegs_all[ps - 1: pe]
        if not jpegs:
            return SegmentResult(error="no pages rendered",
                                 extras={"kind": "pdf"})

        content: list[Any] = [TextBlock(text=(
            f"Question: {question}\n\n"
            f"You are looking at pages {ps}-{ps + len(jpegs) - 1} of a "
            f"scanned PDF. Answer the question concisely, ground every "
            f"claim in what is visible, cite the page number when useful. "
            f"If the answer isn't on these pages, say so plainly."
        ))]
        for offset, jpeg in enumerate(jpegs):
            scaled, media_type = downscale_for_vlm(
                jpeg, max_long_edge=OCR_VLM_MAX_LONG_EDGE,
            )
            content.append(TextBlock(text=f"Page {ps + offset}:"))
            content.append(ImageBlock(
                media_type=media_type,
                data_b64=base64.b64encode(scaled).decode("ascii"),
            ))

        client = get_chat_client("vision")
        try:
            resp = await client.complete(ChatRequest(
                system=(
                    "You answer questions about scanned document pages. "
                    "Be concise and ground every claim in what is visible."
                ),
                messages=[ChatMessage(role="user", content=content)],
                max_tokens=2048,
                temperature=0.2,
            ))
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"VLM call failed: {exc}",
                                 extras={"kind": "pdf", "ocr_indexed": True})
        text = (resp.text or "").strip()
        return SegmentResult(
            text=text or "(VLM returned empty response)",
            extras={
                "kind": "pdf",
                "ocr_indexed": True,
                "vlm_used": True,
                "question": question,
                "page_start": ps,
                "page_end": ps + len(jpegs) - 1,
                "pages_sent": len(jpegs),
            },
        )

    def _slice_ocr_text(
        self,
        file_row: Any,
        args: dict[str, Any],
    ) -> SegmentResult:
        pages, meta = _ocr_pages_from_file(file_row)
        if not pages:
            return SegmentResult(error=(
                "this PDF was OCR-indexed before stored OCR text was available; "
                "pass `question` to query rendered pages via the vision model, "
                "or reprocess the file to build OCR blocks"
            ), extras={"kind": "pdf", "ocr_indexed": True})

        total_indexed_pages = len(pages)
        total_pages = _ocr_total_pages(meta, fallback=total_indexed_pages)
        labels = [str(i) for i in range(1, total_indexed_pages + 1)]
        offset = _int_arg(args.get("offset"), default=0, minimum=0)
        max_chars = _int_arg(
            args.get("max_chars"), default=self.READ_DEFAULT_MAX_CHARS, minimum=1,
        )

        pattern = (args.get("pattern") or "").strip()
        has_page_scope = _has_pdf_page_scope(args)
        if pattern:
            if has_page_scope:
                resolved = _resolve_pdf_page_window(
                    args,
                    total_pages=total_indexed_pages,
                    labels=labels,
                    default_all=True,
                    max_pages=PDF_READ_MAX_PAGES_PER_CALL,
                )
                if isinstance(resolved, SegmentResult):
                    _add_ocr_extras(resolved.extras, meta)
                    return resolved
                scoped_pages = pages[resolved.page_start - 1: resolved.page_end]
                result = _pdf_pattern_search(
                    pages=scoped_pages,
                    pattern=pattern,
                    context_lines=int(args.get("context_lines") or 2),
                    max_matches=int(args.get("max_matches") or 20),
                    match_offset=max(0, int(args.get("match_offset") or 0)),
                    page_offset=resolved.page_start - 1,
                    total_pages_full=total_pages,
                    page_labels=labels[resolved.page_start - 1: resolved.page_end],
                )
                _add_ocr_window_extras(result.extras, resolved, meta)
                return result
            result = _pdf_pattern_search(
                pages=pages,
                pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                total_pages_full=total_pages,
                page_labels=labels,
            )
            _add_ocr_extras(result.extras, meta)
            return result

        section_id = (args.get("section_id") or "").strip()
        heading = (args.get("heading") or "").strip()
        if section_id or heading:
            section = _find_pdf_section(file_row, section_id=section_id, heading=heading)
            if section is None:
                miss = section_id or f"heading={heading!r}"
                return SegmentResult(
                    error=f"section not found: {miss}",
                    extras=_ocr_base_extras(meta),
                )
            window = _page_window_from_section(section, total_pages=total_indexed_pages)
            if window is None:
                summary = str(section.get("summary") or "").strip()
                extras = _ocr_base_extras(meta)
                extras.update({
                    "section_id": section.get("id"),
                    "title": section.get("title"),
                    "summary": summary,
                    "note": "section anchor did not resolve to OCR pages",
                })
                return SegmentResult(text=summary, extras=extras)
            body = _render_ocr_pages(
                pages[window.page_start - 1: window.page_end],
                start_page=window.page_start,
            )
            result = _clamp_pdf(
                body,
                offset,
                max_chars,
                extras={
                    "section_id": section.get("id"),
                    "title": section.get("title"),
                },
            )
            _add_ocr_window_extras(result.extras, window, meta)
            return result

        if has_page_scope:
            resolved = _resolve_pdf_page_window(
                args,
                total_pages=total_indexed_pages,
                labels=labels,
                default_all=False,
                max_pages=PDF_READ_MAX_PAGES_PER_CALL,
            )
            if isinstance(resolved, SegmentResult):
                _add_ocr_extras(resolved.extras, meta)
                return resolved
            body = _render_ocr_pages(
                pages[resolved.page_start - 1: resolved.page_end],
                start_page=resolved.page_start,
            )
            result = _clamp_pdf(body, offset, max_chars)
            _add_ocr_window_extras(result.extras, resolved, meta)
            return result

        body = _render_ocr_pages(pages, start_page=1)
        ps, pe = _page_range_from_offset(body, offset, max_chars, total_indexed_pages)
        result = _clamp_pdf(
            body,
            offset,
            max_chars,
            extras={"page_start": ps, "page_end": pe},
        )
        _add_ocr_extras(result.extras, meta)
        return result

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first variant — used by ArchivePipeline for member peeks."""
        return self._slice(body, args)

    def _slice(
        self, pdf_bytes: bytes, args: dict[str, Any],
    ) -> SegmentResult:
        """Resolve args against a PDF's text body.

        Field priority:
          1. pattern             → regex search across all pages
          2. page_start/page_end → return text for that page range
          3. (default)           → return offset..offset+max_chars of the
                                    full concatenated body

        offset/max_chars further clamp the result of (2).

        When pypdf extracts no text from any page (scanned/image PDF),
        returns an actionable error suggesting `question` for VLM-based
        reading instead of the opaque "empty result".
        """
        return self._slice_text_layer(pdf_bytes, args)

    def _slice_text_layer(
        self, pdf_bytes: bytes, args: dict[str, Any],
    ) -> SegmentResult:
        try:
            labels = extract_pdf_page_labels(pdf_bytes)
            total_pages = len(labels)
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF parse failed: {exc}")
        if total_pages == 0:
            return SegmentResult(error="PDF has no pages")

        offset = _int_arg(args.get("offset"), default=0, minimum=0)
        max_chars = _int_arg(
            args.get("max_chars"), default=self.READ_DEFAULT_MAX_CHARS, minimum=1,
        )

        pattern = (args.get("pattern") or "").strip()
        has_page_scope = _has_pdf_page_scope(args)
        if pattern:
            if has_page_scope:
                resolved = _resolve_pdf_page_window(
                    args,
                    total_pages=total_pages,
                    labels=labels,
                    default_all=True,
                    max_pages=PDF_READ_MAX_PAGES_PER_CALL,
                )
            else:
                end = min(total_pages, PDF_PATTERN_UNSCOPED_MAX_PAGES)
                resolved = _PdfPageWindow(
                    page_start=1,
                    page_end=end,
                    requested_page_end=total_pages,
                    truncated=end < total_pages,
                )
            if isinstance(resolved, SegmentResult):
                return resolved
            doc = extract_pdf_text_range(
                pdf_bytes,
                page_start=resolved.page_start,
                page_end=resolved.page_end,
            )
            if all(not page.strip() for page in doc.pages):
                return SegmentResult(
                    error=_NO_TEXT_LAYER_ERROR,
                    extras={
                        "pattern": pattern,
                        "total_pages": total_pages,
                        "page_start": resolved.page_start,
                        "page_end": resolved.page_end,
                    },
                )
            result = _pdf_pattern_search(
                pages=doc.pages,
                pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                page_offset=doc.page_start - 1,
                total_pages_full=total_pages,
                page_labels=doc.page_labels,
            )
            _add_pdf_window_extras(result.extras, resolved, doc)
            if resolved.truncated:
                result.extras["search_truncated"] = True
                result.extras["hint"] = (
                    "PDF search was capped; use read_entries_metadata sections, "
                    "then pass page_start/page_end."
                )
            return result

        if has_page_scope:
            resolved = _resolve_pdf_page_window(
                args,
                total_pages=total_pages,
                labels=labels,
                default_all=False,
                max_pages=PDF_READ_MAX_PAGES_PER_CALL,
            )
            if isinstance(resolved, SegmentResult):
                return resolved
            doc = extract_pdf_text_range(
                pdf_bytes,
                page_start=resolved.page_start,
                page_end=resolved.page_end,
            )
            if all(not page.strip() for page in doc.pages):
                return SegmentResult(
                    error=_NO_TEXT_LAYER_ERROR,
                    extras={
                        "page_start": doc.page_start,
                        "page_end": doc.page_start + len(doc.pages) - 1,
                        "total_pages": total_pages,
                        "empty_pages_in_range": len(doc.pages),
                    },
                )
            result = _clamp_pdf(
                render_pdf_text_pages(doc),
                offset,
                max_chars,
                extras={"total_pages": total_pages},
            )
            _add_pdf_window_extras(result.extras, resolved, doc)
            return result

        end = min(total_pages, PDF_DEFAULT_READ_PAGES)
        doc = extract_pdf_text_range(pdf_bytes, page_start=1, page_end=end)
        if all(not page.strip() for page in doc.pages):
            return SegmentResult(
                error=_NO_TEXT_LAYER_ERROR,
                extras={"total_pages": total_pages, "page_end": end},
            )
        body = render_pdf_text_pages(doc)
        if offset >= len(body) and end < total_pages:
            return SegmentResult(
                error=(
                    "offset is beyond the default PDF read window; use "
                    "page_start/page_end from metadata sections instead"
                ),
                extras={
                    "total_pages": total_pages,
                    "page_start": 1,
                    "page_end": end,
                    "read_truncated": True,
                    "next_page_start": end + 1,
                },
            )
        ps, pe = _page_range_from_offset(body, offset, max_chars, total_pages)
        result = _clamp_pdf(
            body,
            offset,
            max_chars,
            extras={"total_pages": total_pages, "page_start": ps, "page_end": pe},
        )
        if end < total_pages:
            result.extras.update({
                "read_truncated": True,
                "read_page_end": end,
                "next_page_start": end + 1,
                "hint": (
                    "Only the first PDF page window was extracted; use "
                    "read_entries_metadata sections, then read a targeted "
                    "page_start/page_end window."
                ),
            })
        return result

    @staticmethod
    def _extract_text(
        pdf_bytes: bytes, *, max_pages: int | None = MAX_PAGES,
    ) -> list[str]:
        """Return text per page.

        `max_pages` is only for prompt construction. Readback passes
        `None` so `read_files(page_start=900)` can access late pages.
        """
        doc = extract_pdf_text_range(
            pdf_bytes,
            page_start=1,
            page_end=max_pages,
        )
        return doc.pages

    @staticmethod
    def _truncate(rendered: str) -> str:
        if len(rendered) <= MAX_TOTAL_TEXT_BYTES:
            return rendered
        return rendered[:MAX_TOTAL_TEXT_BYTES] + "\n[...truncated...]"

    @staticmethod
    def _render_for_prompt(text_per_page: list[str]) -> str:
        """Backwards-compatible legacy renderer (no figures). Kept for
        contexts that explicitly want text-only output."""
        chunks: list[str] = []
        size = 0
        for i, t in enumerate(text_per_page, start=1):
            head = f"### Page {i}\n"
            chunk = head + (t.strip() or "(no text on this page)")
            if size + len(chunk) > MAX_TOTAL_TEXT_BYTES:
                truncated = chunk[: MAX_TOTAL_TEXT_BYTES - size]
                chunks.append(truncated + "\n[...truncated...]")
                break
            chunks.append(chunk)
            size += len(chunk)
        return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Image extraction + VLM description
#
# Two responsibilities:
#   (1) Walk the PDF and emit a small list of significant images,
#       filtering icons / decorations.
#   (2) Concurrently describe each image via the `vision` profile.
#
# Failure semantics differ from the main ingest path: a single image
# failing here (VLM timeout, oversize, decode error) degrades to a
# placeholder rather than blocking the surrounding PDF transaction.
# ---------------------------------------------------------------------------

MIN_IMAGE_BYTES = 512
# Pixel-dimension test (>= MIN_IMAGE_PX in both axes) is the primary
# significance filter. The byte test is a backstop catching truly
# trivial extracts (single-color icons compressed to a few hundred bytes
# even at large pixel dims).
MIN_IMAGE_PX = 100
MAX_IMAGES_PER_PAGE = 5
MAX_IMAGES_PER_DOC = 30
VLM_BATCH_SIZE = 5
VLM_TIMEOUT_SECONDS = 30
MAX_IMAGE_BYTES_PER_VLM = 4 * 1024 * 1024  # 4 MB cap per image to VLM

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


FIGURE_DESCRIBE_SYSTEM = (
    "You are Marginalia's figure describer. Given one image extracted from "
    "a PDF, output ONE short paragraph (1-3 sentences) describing what the "
    "image shows. Focus on: figure type (chart/diagram/photo/equation/"
    "table-as-image), the key entities or numbers, and the takeaway. "
    "Do NOT speculate beyond what is visible. Do NOT prefix with 'This "
    "image shows' — just describe directly. Output plain text only."
)


# ---- scanned-PDF OCR via VLM ---------------------------------------------

def _build_ocr_pages_payload(pages: list[str]) -> tuple[list[dict[str, Any]], int]:
    stored: list[dict[str, Any]] = []
    block_count = 0
    for page_no, text in enumerate(pages, start=1):
        clean = (text or "").strip()
        if not clean:
            continue
        blocks = _split_ocr_blocks(clean, page_no=page_no)
        block_count += len(blocks)
        stored.append({
            "page": page_no,
            "text": clean,
            "char_count": len(clean),
            "blocks": blocks,
        })
    return stored, block_count


def _split_ocr_blocks(text: str, *, page_no: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    current_type = "paragraph"

    def flush() -> None:
        nonlocal current
        body = "\n".join(current).strip()
        current = []
        if not body:
            return
        idx = len(blocks) + 1
        if len(body) > OCR_BLOCK_MAX_CHARS:
            body = body[:OCR_BLOCK_MAX_CHARS].rstrip() + "\n[block truncated]"
        blocks.append({
            "id": f"p{page_no}b{idx}",
            "type": current_type,
            "label": _ocr_block_label(body, current_type),
            "text": body,
        })

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            current_type = "paragraph"
            continue
        line_type = _ocr_line_type(stripped)
        if line_type == "heading":
            flush()
            current_type = "heading"
            current = [stripped]
            flush()
            current_type = "paragraph"
            continue
        if line_type != current_type and current:
            flush()
        current_type = line_type
        current.append(line)
    flush()
    return blocks


def _ocr_line_type(line: str) -> str:
    if line.startswith("#"):
        return "heading"
    if line.count("|") >= 2:
        return "table"
    if re.match(r"^\s*(chapter|section|part|appendix)\b", line, re.IGNORECASE):
        return "heading"
    if re.match(r"^\s*\d+(\.\d+){0,3}\s+\S+", line) and len(line) <= 120:
        return "heading"
    return "paragraph"


def _ocr_block_label(text: str, block_type: str) -> str:
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
    if not first:
        return block_type
    return first[:80]


def _classify_ocr_document(pages: list[str], *, total_pages: int) -> str:
    text = "\n".join(pages)
    lower = text.lower()
    colon_lines = sum(1 for ln in text.splitlines() if ":" in ln or "：" in ln)
    table_lines = sum(1 for ln in text.splitlines() if ln.count("|") >= 2)
    heading_hits = len(re.findall(
        r"(^|\n)\s*#{1,4}\s+|(^|\n)\s*(chapter|part|appendix)\b",
        lower,
        flags=re.IGNORECASE,
    ))
    if "invoice" in lower or "receipt" in lower or "发票" in text or "收据" in text:
        return "receipt"
    if table_lines >= 3:
        return "table"
    if colon_lines >= 6 and re.search(
        r"\b(name|date|address|signature|applicant)\b|姓名|日期|地址|签名|申请人",
        text,
        flags=re.IGNORECASE,
    ):
        return "form"
    if total_pages >= 10 and heading_hits >= 2:
        return "book"
    if total_pages >= 20:
        return "long_document"
    return "document"


def _ocr_retrieval_extra(
    *,
    base_extra: str | None,
    ocr_pages: list[str],
    document_type: str,
) -> str:
    lines: list[str] = []
    if base_extra and base_extra.strip():
        lines.append(base_extra.strip())
    lines.append(f"ocr_document_type: {document_type}")
    sample_parts: list[str] = []
    total = 0
    for page_no, text in enumerate(ocr_pages, start=1):
        clean = (text or "").strip()
        if not clean:
            continue
        part = f"[Page {page_no}]\n{clean}"
        if total + len(part) + 2 > OCR_STORED_TEXT_SAMPLE_CHARS:
            remaining = OCR_STORED_TEXT_SAMPLE_CHARS - total
            if remaining > 0:
                sample_parts.append(part[:remaining].rstrip())
            break
        sample_parts.append(part)
        total += len(part) + 2
    if sample_parts:
        lines.append("ocr_text_sample:\n" + "\n\n".join(sample_parts))
    return "\n".join(lines)


def _file_was_ocr_indexed(file_row: Any) -> bool:
    """True iff the ingest pipeline marked this PDF as OCR-only.

    Set by `PdfPipeline.run` when the text-layer extraction came back
    nearly empty and the VLM was used to reconstruct page text. Stored
    as `description.ocr` (a dict carrying engine + page counts).
    """
    desc = getattr(file_row, "description", None)
    return isinstance(desc, dict) and isinstance(desc.get("ocr"), dict)


def _ocr_pages_from_file(file_row: Any) -> tuple[list[str], dict[str, Any]]:
    desc = getattr(file_row, "description", None)
    if not isinstance(desc, dict):
        return [], {}
    meta = desc.get("ocr") if isinstance(desc.get("ocr"), dict) else {}
    raw_pages = desc.get("ocr_pages")
    if not isinstance(raw_pages, list):
        return [], dict(meta)
    max_page = 0
    page_text: dict[int, str] = {}
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        try:
            page_no = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_no <= 0:
            continue
        max_page = max(max_page, page_no)
        page_text[page_no] = str(item.get("text") or "")
    pages = [page_text.get(i, "") for i in range(1, max_page + 1)]
    return pages, dict(meta)


def _render_ocr_pages(pages: list[str], *, start_page: int) -> str:
    chunks: list[str] = []
    for offset, text in enumerate(pages):
        page_no = start_page + offset
        body = (text or "").strip() or "(no OCR text on this page)"
        chunks.append(f"[Page {page_no}]\n{body}")
    return "\n\n".join(chunks)


def _find_pdf_section(
    file_row: Any, *, section_id: str = "", heading: str = "",
) -> dict[str, Any] | None:
    desc = getattr(file_row, "description", None)
    if not isinstance(desc, dict):
        return None
    sections = desc.get("sections")
    if not isinstance(sections, list):
        return None
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if section_id and str(sec.get("id") or "") == section_id:
            return sec
    if heading:
        needle = heading.strip().casefold()
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if str(sec.get("title") or "").strip().casefold() == needle:
                return sec
    return None


def _page_window_from_section(
    section: dict[str, Any], *, total_pages: int,
) -> _PdfPageWindow | None:
    anchor = section.get("anchor") or {}
    if isinstance(anchor, dict):
        value = str(anchor.get("value") or anchor.get("path") or "")
    else:
        value = str(anchor)
    nums = [int(n) for n in re.findall(r"\d+", value)]
    if not nums:
        return None
    start = max(1, min(nums[0], total_pages))
    end = max(start, min(nums[-1], total_pages))
    return _PdfPageWindow(
        page_start=start,
        page_end=end,
        requested_page_end=end,
        truncated=False,
    )


def _ocr_base_extras(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "pdf",
        "ocr_indexed": True,
        "ocr_document_type": meta.get("document_type"),
        "ocr_pages_total": meta.get("pages_total"),
        "ocr_pages_processed": meta.get("pages_processed"),
        "ocr_stored_pages": meta.get("stored_pages"),
    }


def _add_ocr_extras(extras: dict[str, Any], meta: dict[str, Any]) -> None:
    extras.update(_ocr_base_extras(meta))


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _ocr_total_pages(meta: dict[str, Any], *, fallback: int) -> int:
    return _positive_int(meta.get("pages_total")) or fallback


def _add_ocr_window_extras(
    extras: dict[str, Any],
    window: _PdfPageWindow,
    meta: dict[str, Any],
) -> None:
    _add_ocr_extras(extras, meta)
    fallback_total = _positive_int(meta.get("stored_pages")) or window.page_end
    extras.update({
        "page_start": window.page_start,
        "page_end": window.page_end,
        "total_pages": _ocr_total_pages(meta, fallback=fallback_total),
    })
    if window.page_label is not None:
        extras["page_label"] = window.page_label
        extras["resolved_page"] = window.resolved_page
    if window.truncated:
        extras["window_truncated"] = True
        extras["requested_page_end"] = window.requested_page_end


async def _ocr_pdf_pages(pdf_bytes: bytes, total_pages: int) -> list[str]:
    """Render the first OCR_MAX_PAGES pages to JPEG via pypdfium2,
    down-scale each via downscale_for_vlm, and ask the vision profile
    to extract text in markdown. Returns one entry per rendered page;
    pages beyond the cap are returned as empty strings.

    Empty / "No text content" responses are normalised to '' so the
    caller can detect the all-empty-page case and raise PdfNeedsOcrError.
    """
    pages_to_ocr = min(total_pages, OCR_MAX_PAGES)
    page_jpegs = await asyncio.to_thread(
        _render_pdf_pages_to_jpeg, pdf_bytes, pages_to_ocr,
    )
    client = get_chat_client("vision")
    out: list[str] = [""] * len(page_jpegs)
    sem = asyncio.Semaphore(llm_ingest_concurrency())

    async def _ocr_one(i: int, jpeg_bytes: bytes) -> None:
        try:
            async with sem:
                # OCR is more sensitive to fine glyph detail than image
                # caption, so use a higher long-edge cap than the caption
                # path. 200-DPI A4 renders to ~2200px and only loses ~7%
                # at 2048; 8pt footnotes in dense layouts stay readable.
                scaled, media_type = downscale_for_vlm(
                    jpeg_bytes, max_long_edge=OCR_VLM_MAX_LONG_EDGE,
                )
                b64 = base64.b64encode(scaled).decode("ascii")
                resp = await client.complete(ChatRequest(
                    system=PDF_OCR_PROMPT,
                    messages=[ChatMessage(role="user", content=[
                        TextBlock(text=f"Page {i + 1} of {pages_to_ocr}."),
                        ImageBlock(media_type=media_type, data_b64=b64),
                    ])],
                    max_tokens=4096,
                    temperature=0.0,
                ))
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR call failed for page %d: %s", i + 1, exc)
            return
        text = (resp.text or "").strip()
        if text.lower() in ("no text content", "no text content."):
            text = ""
        out[i] = text

    await asyncio.gather(*(
        _ocr_one(i, jpeg_bytes) for i, jpeg_bytes in enumerate(page_jpegs)
    ))
    # Pad with empties for pages we skipped past the cap, so caller's
    # page indexing stays aligned with total_pages.
    while len(out) < total_pages:
        out.append("")
    return out


def _render_pdf_pages_to_jpeg(
    pdf_bytes: bytes, page_count: int,
) -> list[bytes]:
    """Render `page_count` pages to JPEG bytes. Sync, intended to run
    inside asyncio.to_thread. Mirrors WeKnora's PDFScannedParser shape:
    pypdfium2 → PIL.Image → JPEG via Pillow."""
    import pypdfium2 as pdfium

    scale = OCR_RENDER_DPI / 72
    out: list[bytes] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for i in range(min(page_count, len(pdf))):
            page = pdf[i]
            bitmap = None
            try:
                bitmap = page.render(scale=scale)
                img = bitmap.to_pil()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85, optimize=True)
                out.append(buf.getvalue())
            finally:
                if bitmap is not None:
                    close = getattr(bitmap, "close", None)
                    if close:
                        close()
                close = getattr(page, "close", None)
                if close:
                    close()
    finally:
        close = getattr(pdf, "close", None)
        if close:
            close()
    return out


@dataclass(slots=True)
class ExtractedImage:
    page_num: int       # 1-indexed
    fig_index: int      # 1-indexed within the page
    media_type: str
    data: bytes
    width: int
    height: int


@dataclass(slots=True)
class DescribedImage:
    page_num: int
    fig_index: int
    description: str
    error: str | None = None


def extract_images(
    pdf_bytes: bytes, *, max_pages: int | None = None,
) -> list[ExtractedImage]:
    """Walk the PDF and return significant images (icons filtered)."""
    from pypdf import PdfReader  # imported lazily

    out: list[ExtractedImage] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        log.exception("pypdf failed to open PDF for image extraction")
        return out

    total = 0
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    for page_num, page in enumerate(pages, start=1):
        try:
            page_images = list(page.images)[:MAX_IMAGES_PER_PAGE]
        except Exception as exc:
            # Common when Pillow isn't installed: pypdf can't decode the
            # image stream and raises. Once per-page is too noisy at WARNING.
            log.debug("pypdf failed listing images on page %d: %s",
                      page_num, exc)
            continue

        page_kept = 0
        for fig_idx, img in enumerate(page_images, start=1):
            data = img.data or b""
            if len(data) < MIN_IMAGE_BYTES:
                continue

            width = height = 0
            try:
                pil = img.image
                if pil is not None:
                    width, height = pil.size
            except Exception:
                pass
            if width and height:
                if width < MIN_IMAGE_PX or height < MIN_IMAGE_PX:
                    continue

            ext = (img.name or "").rsplit(".", 1)[-1].lower()
            media_type = _MIME_BY_EXT.get(ext, "image/png")

            out.append(ExtractedImage(
                page_num=page_num,
                fig_index=page_kept + 1,
                media_type=media_type,
                data=data[:MAX_IMAGE_BYTES_PER_VLM],
                width=width, height=height,
            ))
            page_kept += 1
            total += 1
            if total >= MAX_IMAGES_PER_DOC:
                return out
    return out


async def describe_images(
    images: list[ExtractedImage],
) -> list[DescribedImage]:
    """Send each image through the vision profile concurrently."""
    if not images:
        return []
    client = get_chat_client("vision")
    out: list[DescribedImage] = []

    for batch_start in range(0, len(images), VLM_BATCH_SIZE):
        batch = images[batch_start : batch_start + VLM_BATCH_SIZE]
        results = await asyncio.gather(
            *(_describe_one(client, img) for img in batch),
            return_exceptions=True,
        )
        for img, res in zip(batch, results):
            if isinstance(res, BaseException):
                log.warning("VLM describe failed for fig %d.%d: %r",
                            img.page_num, img.fig_index, res)
                out.append(DescribedImage(
                    page_num=img.page_num, fig_index=img.fig_index,
                    description="(figure description unavailable)",
                    error=repr(res),
                ))
            else:
                out.append(res)
    return out


async def _describe_one(client, img: ExtractedImage) -> DescribedImage:
    scaled, media_type = downscale_for_vlm(img.data)
    b64 = base64.b64encode(scaled).decode("ascii")
    user_text = (
        f"Figure on page {img.page_num} (fig {img.fig_index}) of a PDF. "
        f"Describe in 1-3 sentences."
    )
    request = ChatRequest(
        system=FIGURE_DESCRIBE_SYSTEM,
        messages=[ChatMessage(role="user", content=[
            TextBlock(text=user_text),
            ImageBlock(media_type=media_type, data_b64=b64),
        ])],
        max_tokens=300,
        temperature=0.2,
    )
    try:
        resp = await asyncio.wait_for(
            client.complete(request), timeout=VLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return DescribedImage(
            page_num=img.page_num, fig_index=img.fig_index,
            description="(figure description timed out)",
            error="timeout",
        )
    text = (resp.text or "").strip() or "(empty VLM response)"
    return DescribedImage(
        page_num=img.page_num, fig_index=img.fig_index,
        description=text,
    )


def render_pages_with_figures(
    text_per_page: list[str],
    described: list[DescribedImage],
    *,
    start_page: int = 1,
) -> str:
    """Build the prompt body, with `[Figure X.Y] ...` lines appended to
    each page's text block."""
    by_page: dict[int, list[DescribedImage]] = {}
    for d in described:
        by_page.setdefault(d.page_num, []).append(d)

    chunks: list[str] = []
    for i, t in enumerate(text_per_page, start=start_page):
        body = (t or "").strip() or "(no text on this page)"
        figs = by_page.get(i, [])
        if figs:
            fig_lines = [
                f"[Figure {f.page_num}.{f.fig_index}] {f.description}"
                for f in figs
            ]
            body = body + "\n\n" + "\n".join(fig_lines)
        chunks.append(f"### Page {i}\n{body}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# read_segment helpers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _PdfPageWindow:
    page_start: int
    page_end: int
    requested_page_end: int
    truncated: bool = False
    page_label: str | None = None
    resolved_page: int | None = None


def _int_arg(value: Any, *, default: int, minimum: int | None = None) -> int:
    if value in (None, ""):
        parsed = default
    else:
        parsed = int(value)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _has_pdf_page_scope(args: dict[str, Any]) -> bool:
    return any(
        args.get(key) not in (None, "")
        for key in ("page_start", "page_end", "page_label")
    )


def _resolve_pdf_page_window(
    args: dict[str, Any],
    *,
    total_pages: int,
    labels: list[str],
    default_all: bool,
    max_pages: int,
) -> _PdfPageWindow | SegmentResult:
    try:
        page_label_raw = args.get("page_label")
        if page_label_raw not in (None, ""):
            resolved = resolve_page_label(labels, page_label_raw)
            if resolved is None:
                return SegmentResult(
                    error="page_label was not found in PDF page labels",
                    extras={
                        "page_label": str(page_label_raw),
                        "total_pages": total_pages,
                    },
                )
            start = resolved
            end = _int_arg(args.get("page_end"), default=start, minimum=start)
            requested_end = min(end, total_pages)
            end = min(requested_end, start + max_pages - 1)
            return _PdfPageWindow(
                page_start=start,
                page_end=end,
                requested_page_end=requested_end,
                truncated=end < requested_end,
                page_label=str(page_label_raw),
                resolved_page=resolved,
            )

        start = _int_arg(args.get("page_start"), default=1, minimum=1)
        default_end = total_pages if default_all else start
        end = _int_arg(args.get("page_end"), default=default_end, minimum=start)
    except (TypeError, ValueError):
        return SegmentResult(
            error="page_start/page_end/page_label must identify PDF pages",
            extras={"total_pages": total_pages},
        )

    start = max(1, min(start, total_pages))
    requested_end = max(start, min(end, total_pages))
    capped_end = min(requested_end, start + max_pages - 1)
    return _PdfPageWindow(
        page_start=start,
        page_end=capped_end,
        requested_page_end=requested_end,
        truncated=capped_end < requested_end,
    )


def _add_pdf_window_extras(
    extras: dict[str, Any],
    window: _PdfPageWindow,
    doc: Any,
) -> None:
    page_end = doc.page_start + len(doc.pages) - 1 if doc.pages else doc.page_start
    extras.update({
        "page_start": doc.page_start,
        "page_end": page_end,
        "total_pages": doc.total_pages,
    })
    if doc.page_labels:
        extras["page_label_start"] = doc.page_labels[0]
        extras["page_label_end"] = doc.page_labels[-1]
    if window.page_label is not None:
        extras["page_label"] = window.page_label
        extras["resolved_page"] = window.resolved_page
    if window.truncated:
        extras["window_truncated"] = True
        extras["requested_page_end"] = window.requested_page_end


_PAGE_MARKER_RE = re.compile(r"\[Page (\d+)\]")


def _page_range_from_offset(
    body: str, offset: int, max_chars: int, total_pages: int,
) -> tuple[int, int]:
    """Given a char offset in the concatenated PDF body (with [Page N]
    markers), find the page_start and page_end for the chunk that would
    be read at that offset."""
    # Find all [Page N] marker positions.
    markers = [(m.start(), int(m.group(1))) for m in _PAGE_MARKER_RE.finditer(body)]
    if not markers:
        return 1, total_pages
    # page_start: the last marker whose position <= offset.
    ps = 1
    for pos, pn in markers:
        if pos <= offset:
            ps = pn
        else:
            break
    # page_end: find the last marker whose position < offset + max_chars.
    end = offset + max_chars
    pe = ps
    for pos, pn in markers:
        if pos < end:
            pe = pn
        else:
            break
    return ps, pe


def _clamp_pdf(
    text: str, offset: int, max_chars: int,
    *, extras: dict[str, Any] | None = None,
) -> SegmentResult:
    extras = dict(extras or {})
    total = len(text)
    chunk = text[offset: offset + max_chars]
    truncated = (offset + len(chunk)) < total
    extras.update({
        "offset": offset,
        "char_count": len(chunk),
        "total_chars": total,
        "truncated": truncated,
    })
    if truncated:
        extras["next_offset"] = offset + len(chunk)
    if not chunk:
        return SegmentResult(text="", error="empty result", extras=extras)
    return SegmentResult(text=chunk, extras=extras)


def _pdf_pattern_search(
    *, pages: list[str], pattern: str,
    context_lines: int, max_matches: int,
    match_offset: int = 0, page_offset: int = 0,
    total_pages_full: int | None = None,
    page_labels: list[str] | None = None,
) -> SegmentResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return SegmentResult(error=f"invalid regex: {exc}")

    full_total_pages = total_pages_full if total_pages_full is not None else len(pages)

    all_hits: list[dict[str, Any]] = []
    for idx, page_text in enumerate(pages):
        if not page_text:
            continue
        page_no = idx + 1 + page_offset
        label = page_labels[idx] if page_labels and idx < len(page_labels) else None
        page_lines = page_text.splitlines()
        for m in rx.finditer(page_text):
            line_no = page_text.count("\n", 0, m.start()) + 1
            s = max(0, line_no - 1 - context_lines)
            e = min(len(page_lines), line_no + context_lines)
            hit = {
                "page": page_no,
                "line": line_no,
                "match": m.group(0)[:200],
                "context": "\n".join(page_lines[s:e]),
            }
            if label is not None:
                hit["page_label"] = label
            all_hits.append(hit)

    total = len(all_hits)
    hits = all_hits[match_offset: match_offset + max_matches]
    has_more = (match_offset + len(hits)) < total

    extras: dict[str, Any] = {
        "pattern": pattern,
        "match_count": len(hits),
        "total_matches": total,
        "match_offset": match_offset,
        "has_more": has_more,
        "hits": hits,
        "total_pages": full_total_pages,
    }
    if page_offset:
        extras["scope_page_start"] = page_offset + 1
        extras["scope_page_end"] = page_offset + len(pages)
    if has_more:
        extras["next_match_offset"] = match_offset + len(hits)

    if not hits:
        if match_offset and total:
            err = f"match_offset {match_offset} exceeds total_matches {total}"
        else:
            err = "no matches"
        return SegmentResult(text="", error=err, extras=extras)

    rendered_lines: list[str] = []
    for h in hits:
        label = h.get("page_label")
        label_text = f" label {label}" if label and label != str(h["page"]) else ""
        rendered_lines.append(
            f"[Page {h['page']}{label_text} L{h['line']}] "
            f"{h['match']}\n  > {h['context']}"
        )
    return SegmentResult(text="\n\n".join(rendered_lines), extras=extras)
