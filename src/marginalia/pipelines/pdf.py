"""PDF pipeline (DESIGN.md §11.3).

Handles application/pdf and `.pdf`. Strategy: pypdf extracts the text
layer page by page; significant images are concurrently described by
the vision LLM and inlined as `[Figure N.M] ...` lines next to their
pages; the assembled body then goes through the same tagged-response
indexing prompt as the text pipeline, but with page anchors in
`<sections>`.

PDFs without a text layer (scanned images) are flagged via a clean
error in the pipeline output — the handler marks the file as needing
OCR. An OCR / vision-per-page pipeline is on the next-cycle list.

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
    parse_path,
    parse_sections,
    parse_tagged,
    parse_tags,
    render_format_hint,
    render_sections_hint,
)
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    SegmentResult,
    TagSuggestion,
)
from marginalia.pipelines.image import downscale_for_vlm
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_PAGES = 60                    # cap pages we feed the model
MAX_TOTAL_TEXT_BYTES = 80_000     # ≈ 25-30k tokens cap
MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER = 50  # if every page yields fewer chars,
                                       # the doc is probably scanned
OCR_MAX_PAGES = 50                # cap how many pages we OCR per doc
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

You receive the full text of a PDF, page-by-page. Produce a structured
index that lets a downstream agent decide whether to retrieve the document
and find the relevant page.

`summary` (2-4 sentences in the document's own language) is content-focused.
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

        vlm_available = has_vision_profile()

        total_pages = len(text_per_page)
        total_chars = sum(len(t) for t in text_per_page)
        ocr_used = False
        ocr_pages_done = 0
        if total_pages > 0 and total_chars / max(total_pages, 1) < MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER:
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
                total_chars / max(total_pages, 1),
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
            "ocr_used": ocr_used,
            "ocr_pages_done": ocr_pages_done if ocr_used else 0,
        }
        user_text = (
            "Index the PDF below. Hints are advisory; the document's text "
            "and figure captions take precedence.\n\n"
            f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body_text}\n</document>"
        )

        client = get_chat_client("ingest")
        max_out = min(8192, max(2048, len(body_text) // 8))
        resp = await client.complete(ChatRequest(
            system=PDF_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
            max_tokens=max_out,
            temperature=0.2,
        ))

        tagged = parse_tagged(resp.text or "")
        summary = tagged.get("summary", "").strip()
        if not summary:
            log.warning(
                "pdf pipeline: no <summary> in response. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("pdf pipeline produced empty summary")

        sections = parse_sections(
            tagged.get("sections", ""), anchor_unit="pages",
        )
        description: dict[str, Any] = {"sections": sections}
        description_text = tagged.get("description", "").strip()
        if description_text:
            description["text"] = description_text
        if ocr_used:
            description["ocr"] = {
                "engine": "vlm",
                "pages_total": total_pages,
                "pages_processed": ocr_pages_done,
            }
        return PipelineResult(
            summary=summary,
            description=description,
            kind="text",
            extra=tagged.get("extra", "").strip() or None,
            entry_extra=tagged.get("entry_extra", "").strip() or None,
            entry_catalog_path=parse_path(tagged.get("catalog_path", "")) or None,
            entry_tags=[
                TagSuggestion(name=t["name"], facet=t["facet"])
                for t in parse_tags(tagged.get("tags", ""))
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

    # ---- read_segment -----------------------------------------------------

    READ_DEFAULT_MAX_CHARS = 8000

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        pdf_bytes = await self._read_bytes(storage, file_row.storage_key)
        return self._slice(pdf_bytes, args)

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
        """
        try:
            pages = self._extract_text(pdf_bytes)
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF parse failed: {exc}")
        total_pages = len(pages)
        body = "\n\n".join(
            f"[Page {i+1}]\n{txt}" for i, txt in enumerate(pages) if txt
        )

        offset = max(0, int(args.get("offset") or 0))
        max_chars = int(args.get("max_chars") or self.READ_DEFAULT_MAX_CHARS)
        if max_chars <= 0:
            max_chars = self.READ_DEFAULT_MAX_CHARS

        pattern = (args.get("pattern") or "").strip()
        if pattern:
            return _pdf_pattern_search(
                pages=pages, pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
            )

        page_start = args.get("page_start")
        page_end = args.get("page_end")
        if page_start:
            try:
                ps = int(page_start)
            except (TypeError, ValueError):
                return SegmentResult(error="page_start must be an integer")
            try:
                pe = int(page_end) if page_end else ps
            except (TypeError, ValueError):
                return SegmentResult(error="page_end must be an integer")
            if total_pages == 0:
                return SegmentResult(error="PDF has no pages")
            ps = max(1, min(ps, total_pages))
            pe = max(ps, min(pe, total_pages))
            slab = "\n\n".join(
                f"[Page {i}]\n{pages[i-1]}" for i in range(ps, pe + 1)
                if pages[i-1]
            )
            return _clamp_pdf(
                slab, offset, max_chars,
                extras={
                    "page_start": ps, "page_end": pe,
                    "total_pages": total_pages,
                },
            )

        return _clamp_pdf(
            body, offset, max_chars,
            extras={"total_pages": total_pages},
        )

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
    out: list[str] = []
    for i, jpeg_bytes in enumerate(page_jpegs):
        # OCR is more sensitive to fine glyph detail than image caption,
        # so use a higher long-edge cap than the caption path. 200-DPI A4
        # renders to ~2200px and only loses ~7% at 2048; 8pt footnotes
        # in dense layouts stay readable.
        scaled, media_type = downscale_for_vlm(
            jpeg_bytes, max_long_edge=OCR_VLM_MAX_LONG_EDGE,
        )
        b64 = base64.b64encode(scaled).decode("ascii")
        try:
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
            out.append("")
            continue
        text = (resp.text or "").strip()
        if text.lower() in ("no text content", "no text content."):
            text = ""
        out.append(text)
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


def extract_images(pdf_bytes: bytes) -> list[ExtractedImage]:
    """Walk the PDF and return significant images (icons filtered)."""
    from pypdf import PdfReader  # imported lazily

    out: list[ExtractedImage] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        log.exception("pypdf failed to open PDF for image extraction")
        return out

    total = 0
    for page_num, page in enumerate(reader.pages, start=1):
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
) -> str:
    """Build the prompt body, with `[Figure X.Y] ...` lines appended to
    each page's text block."""
    by_page: dict[int, list[DescribedImage]] = {}
    for d in described:
        by_page.setdefault(d.page_num, []).append(d)

    chunks: list[str] = []
    for i, t in enumerate(text_per_page, start=1):
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
) -> SegmentResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return SegmentResult(error=f"invalid regex: {exc}")

    hits: list[dict[str, Any]] = []
    for page_no, page_text in enumerate(pages, start=1):
        if not page_text:
            continue
        page_lines = page_text.splitlines()
        for m in rx.finditer(page_text):
            if len(hits) >= max_matches:
                break
            line_no = page_text.count("\n", 0, m.start()) + 1
            s = max(0, line_no - 1 - context_lines)
            e = min(len(page_lines), line_no + context_lines)
            hits.append({
                "page": page_no,
                "line": line_no,
                "match": m.group(0)[:200],
                "context": "\n".join(page_lines[s:e]),
            })
        if len(hits) >= max_matches:
            break

    if not hits:
        return SegmentResult(
            text="", error="no matches",
            extras={"pattern": pattern, "total_pages": len(pages)},
        )

    rendered = "\n\n".join(
        f"[Page {h['page']} L{h['line']}] {h['match']}\n  ┊ {h['context']}"
        for h in hits
    )
    return SegmentResult(
        text=rendered,
        extras={
            "pattern": pattern,
            "match_count": len(hits),
            "hits": hits,
            "total_pages": len(pages),
        },
    )
