"""PDF image extraction + VLM description (Cycle 17b).

Sub-module of pipelines/pdf.py — kept separate to stay under the file size
budget. Two responsibilities:

  1. Walk a PDF's pages and emit a small list of significant images,
     filtering out icons / decorations.
  2. Concurrently describe each image via the `vision` profile, returning
     a (page_num, fig_idx, description) tuple per image.

Failures on a single image (storage decode error, VLM timeout, oversize
image rejection) degrade to a placeholder description so the surrounding
PDF ingest is never blocked.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from dataclasses import dataclass

from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    get_chat_client,
)

log = logging.getLogger(__name__)

MIN_IMAGE_BYTES = 512
# Pixel-dimension test (>= MIN_IMAGE_PX in both axes) is the primary
# significance filter. The byte test is a backstop that catches truly
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
        except Exception:
            log.exception("pypdf failed listing images on page %d", page_num)
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
    b64 = base64.b64encode(img.data).decode("ascii")
    user_text = (
        f"Figure on page {img.page_num} (fig {img.fig_index}) of a PDF. "
        f"Describe in 1-3 sentences."
    )
    request = ChatRequest(
        system=FIGURE_DESCRIBE_SYSTEM,
        messages=[ChatMessage(role="user", content=[
            TextBlock(text=user_text),
            ImageBlock(media_type=img.media_type, data_b64=b64),
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
