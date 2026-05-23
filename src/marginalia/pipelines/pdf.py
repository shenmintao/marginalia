"""PDF pipeline (design.md §11.3, V1 direct-read only).

Handles application/pdf and `.pdf`. V1 strategy: extract text via pypdf,
emit per-page concatenated text, then run the same JSON-schema ingest
prompt as the text pipeline but with a page-aware section schema.

PDFs without a text layer (scanned images) are flagged via a clean error
in the pipeline output — the handler will mark the file as needing OCR.
The actual OCR / vision-per-page path is V2 (Cycle 17b).
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    get_chat_client,
)
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    TagSuggestion,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_PAGES = 60                    # cap pages we feed the model
MAX_TOTAL_TEXT_BYTES = 80_000     # ≈ 25-30k tokens cap
MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER = 50  # if every page yields fewer chars,
                                       # the doc is probably scanned


PDF_PIPELINE_SYSTEM = """You are Marginalia's PDF document indexer.

You receive the full text of a PDF, page-by-page. Produce a structured
index that lets a downstream agent decide whether to retrieve the document
and find the relevant page.

Rules:
- Output ONLY one JSON object matching the provided schema. No prose, no fences.
- `summary`: 2-4 sentences in the document's own language, content-focused.
- `description.sections`: every meaningful section/heading. For each:
  a stable id (s1, s2, …), the heading title, an anchor with
  `unit: "pages"` and `value: "<start>-<end>"` (1-indexed inclusive),
  a 1-2 sentence summary, and 3-7 key terms.
- `kind`: "text".
- `extra`: at most 1 paragraph of cross-cutting insight; "" if nothing notable.
- `entry_extra`: at most 1 paragraph of position-aware insight; "" if none.
- `entry_catalog_path`: best-guess classification path as a list of names.
- `entry_tags`: 3-10 tags. Each `{name, facet}`. Facets:
  topic | form | time | source | language | extra.
"""


PDF_PIPELINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary", "description", "kind", "extra",
        "entry_extra", "entry_catalog_path", "entry_tags",
    ],
    "properties": {
        "summary": {"type": "string"},
        "description": {
            "type": "object",
            "additionalProperties": False,
            "required": ["sections"],
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "title", "anchor", "summary", "key_terms"],
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "anchor": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["unit", "value"],
                                "properties": {
                                    "unit": {"type": "string", "enum": ["pages"]},
                                    "value": {"type": "string"},
                                },
                            },
                            "summary": {"type": "string"},
                            "key_terms": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
        },
        "kind": {"type": "string", "enum": ["text"]},
        "extra": {"type": "string"},
        "entry_extra": {"type": "string"},
        "entry_catalog_path": {"type": "array", "items": {"type": "string"}},
        "entry_tags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "facet"],
                "properties": {
                    "name": {"type": "string"},
                    "facet": {
                        "type": "string",
                        "enum": ["topic", "form", "time", "source",
                                "language", "extra"],
                    },
                },
            },
        },
    },
}


class PdfNeedsOcrError(Exception):
    """Raised when the PDF appears to be scanned (no usable text layer).

    The handler catches this and marks `files.ingest_status='failed'` with
    reason 'needs_ocr' so a future OCR / vision-per-page pipeline can take
    over without re-uploading."""

    def __init__(self, *, total_pages: int, total_chars: int) -> None:
        super().__init__(
            f"PDF has no usable text layer "
            f"(pages={total_pages}, chars={total_chars}); needs OCR."
        )
        self.total_pages = total_pages
        self.total_chars = total_chars


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
        text_per_page = self._extract_text(body)

        total_pages = len(text_per_page)
        total_chars = sum(len(t) for t in text_per_page)
        if total_pages > 0 and total_chars / max(total_pages, 1) < MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER:
            raise PdfNeedsOcrError(
                total_pages=total_pages, total_chars=total_chars,
            )

        # Extract embedded figures and describe them via vision profile.
        # Single-image failures degrade to placeholder text; the ingest
        # call below still gets useful context.
        from marginalia.pipelines.pdf_images import (
            describe_images, extract_images, render_pages_with_figures,
        )

        images = extract_images(body)
        described = await describe_images(images) if images else []
        body_text = render_pages_with_figures(text_per_page, described)
        body_text = self._truncate(body_text)

        user_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "page_count": total_pages,
            "figure_count": len(described),
        }
        user_text = (
            "Index the PDF below. Hints are advisory; the document's text "
            "and figure captions take precedence.\n\n"
            f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body_text}\n</document>"
        )

        client = get_chat_client("ingest")
        max_out = min(4096, max(1024, len(body_text) // 12))
        resp = await client.complete(ChatRequest(
            system=PDF_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
            max_tokens=max_out,
            json_schema=PDF_PIPELINE_SCHEMA,
            temperature=0.2,
        ))

        if resp.parsed_json is None:
            log.warning(
                "pdf pipeline: model did not return parseable JSON. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("pdf pipeline produced non-JSON output")

        data = resp.parsed_json
        return PipelineResult(
            summary=str(data["summary"]),
            description={"sections": data["description"]["sections"]},
            kind="text",
            extra=(data.get("extra") or "") or None,
            entry_extra=(data.get("entry_extra") or "") or None,
            entry_catalog_path=list(data.get("entry_catalog_path") or []) or None,
            entry_tags=[
                TagSuggestion(name=str(t["name"]), facet=str(t["facet"]))
                for t in (data.get("entry_tags") or [])
            ],
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
    def _extract_text(pdf_bytes: bytes) -> list[str]:
        """Return text per page, capped at MAX_PAGES."""
        from pypdf import PdfReader  # imported lazily so the package is optional
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = reader.pages[:MAX_PAGES]
        out: list[str] = []
        for p in pages:
            try:
                txt = p.extract_text() or ""
            except Exception:  # noqa: BLE001 — pypdf occasionally throws
                txt = ""
            out.append(txt)
        return out

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
