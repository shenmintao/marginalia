"""Image pipeline (DESIGN.md §11.3).

Handles raster images: image/png / image/jpeg / image/gif / image/webp.
Uses the `vision` LLM profile (a multimodal model) and feeds the image
bytes as a base64 ImageBlock — the abstraction layer translates to each
provider's native shape (OpenAI: data: URL; Anthropic: base64 source).

Single LLM call producing structured JSON: a description of the image's
content, key regions / objects, suggested catalog placement, and tags.

Before any image hits the VLM we down-scale to `VLM_MAX_LONG_EDGE` on
the long edge (Anthropic vision sweet spot — clear without burning
tokens) and re-encode as JPEG quality 85. Already-small images pass
through unchanged. This keeps screenshots and chat captures readable
(14px font @1568 long edge ≈ 11px after rescale, comfortably above the
~10px VLM threshold) without making 4K screen captures expensive.
"""
from __future__ import annotations

import base64
import io
import json
import logging
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
    parse_tagged,
    parse_tags,
    render_format_hint,
)
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    SegmentResult,
    TagSuggestion,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB cap (most VLMs reject larger)
VLM_MAX_LONG_EDGE = 1568            # Anthropic vision sweet spot
VLM_JPEG_QUALITY = 85


def downscale_for_vlm(
    body: bytes,
    *,
    max_long_edge: int = VLM_MAX_LONG_EDGE,
) -> tuple[bytes, str]:
    """Return (jpeg_bytes, 'image/jpeg') after down-scaling to fit
    `max_long_edge`. Already-small images are re-encoded as JPEG too —
    this gives a single, predictable shape going to every VLM provider
    (PNG → JPEG saves ~3-5x on screenshots) at the cost of one Pillow
    round-trip when the image is already within bounds.

    On any Pillow failure (corrupt input, exotic format) returns the
    original bytes + image/png as a best-effort fallback.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return body, "image/png"
    try:
        img = Image.open(io.BytesIO(body))
        img.load()
    except Exception:  # noqa: BLE001 — Pillow surfaces many error types
        return body, "image/png"

    # Drop alpha to keep JPEG happy.
    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    longest = max(img.size)
    if longest > max_long_edge:
        img.thumbnail((max_long_edge, max_long_edge), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=VLM_JPEG_QUALITY, optimize=True)
    return out.getvalue(), "image/jpeg"


IMAGE_PIPELINE_SYSTEM = """You are Marginalia's image indexer.

Your job: look at one image and produce a structured index that lets a
downstream agent decide whether to retrieve it.

`summary` is one or two sentences (≤60 中文字 / ≤30 English words) in the
user's likely language — the spine of what the image is. Keep it tight;
detail belongs in `description`. `description` is a free-text walk-through of what the
image shows — visible text, key objects, layout — multi-paragraph if
useful. `extra` carries machine-friendly insights as `key: value` lines
(one per line; keys like `primary_color`, `detected_text_lang`,
`dominant_subject`, `quality`, `notable`); leave the block empty if
there is nothing notable. `entry_extra` is the same shape but for
position-aware insights. `entry_catalog_path` is a best-guess
classification path. `tags` are 3-10 facet:name pairs; valid facets are
topic | form | time | source | language | extra.

""" + render_format_hint(kinds=("image",))


# Schema dict kept for legacy callers but no longer fed to the LLM.
IMAGE_PIPELINE_SCHEMA: dict[str, Any] = {}


@register_pipeline(
    mimes=("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"),
    mime_prefixes=("image/",),
    exts=(".png", ".jpg", ".jpeg", ".gif", ".webp"),
)
class ImagePipeline(Pipeline):
    name = "image"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        if not has_vision_profile():
            # Image indexing fundamentally needs a VLM. Raise a clean
            # message so the file is marked ingest_status='failed' with
            # a reason the user can act on.
            raise RuntimeError(
                "image pipeline requires the `vision` LLM profile; "
                "set LLM_VISION_API_KEY (or LLM_DEFAULT_API_KEY for a VLM "
                "provider) and re-ingest."
            )
        body = await self._read_bytes(storage, ctx.storage_key)
        scaled, media_type = downscale_for_vlm(body)
        b64 = base64.b64encode(scaled).decode("ascii")

        user_text = (
            "Index the image below. Hints are advisory; the image's actual "
            "content takes precedence.\n\n"
            f"<context>\n{json.dumps({k: v for k, v in {'folder_path': ctx.folder_path, 'sibling_names': ctx.sibling_names, 'catalog_sketch': ctx.catalog_sketch, 'tag_vocabulary': ctx.tag_vocabulary}.items()}, ensure_ascii=False)}\n</context>"
        )

        client = get_chat_client("vision")
        resp = await client.complete(ChatRequest(
            system=IMAGE_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[
                TextBlock(text=user_text),
                ImageBlock(media_type=media_type, data_b64=b64),
            ])],
            max_tokens=4096,
            temperature=0.2,
        ))

        tagged = parse_tagged(resp.text or "")
        summary = tagged.get("summary", "").strip()
        if not summary:
            log.warning(
                "image pipeline: no <summary> in response. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("image pipeline produced empty summary")

        description_text = tagged.get("description", "").strip()
        return PipelineResult(
            summary=summary,
            description={"text": description_text} if description_text else {},
            kind="image",
            extra=tagged.get("extra", "").strip() or None,
            entry_extra=tagged.get("entry_extra", "").strip() or None,
            entry_catalog_path=parse_path(tagged.get("catalog_path", "")) or None,
            entry_tags=[
                TagSuggestion(name=t["name"], facet=t["facet"])
                for t in parse_tags(tagged.get("tags", ""))
            ],
        )

    @staticmethod
    async def _read_bytes(storage: StorageBackend, key: str) -> bytes:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
            if len(buf) > MAX_IMAGE_BYTES:
                buf = bytearray(buf[:MAX_IMAGE_BYTES])
                break
        return bytes(buf)

    # ---- read_segment -----------------------------------------------------

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Two modes, picked by whether `args["question"]` is set.

        With `question`: send the original image (downscaled) to the VLM
        with the agent's question and return the VLM's targeted answer.
        This is the right shape — an image's "body" is the image itself,
        and the ingest-time summary is a frozen high-level index, not a
        substitute for actually looking at the picture when the agent
        has a specific question.

        Without `question`: fall back to rendering the persisted summary
        + description as text. Generic offset / max_chars apply for
        chunked reads of long descriptions.
        """
        question = (args.get("question") or "").strip() if isinstance(args, dict) else ""
        if question:
            return await self._answer_with_vlm(
                file_row=file_row, question=question, storage=storage,
            )
        body = _render_image_description(file_row)
        offset = max(0, int(args.get("offset") or 0))
        max_chars = int(args.get("max_chars") or 8000)
        if max_chars <= 0:
            max_chars = 8000
        total = len(body)
        chunk = body[offset: offset + max_chars]
        truncated = (offset + len(chunk)) < total
        extras: dict[str, Any] = {
            "offset": offset,
            "char_count": len(chunk),
            "total_chars": total,
            "truncated": truncated,
            "kind": "image",
        }
        if truncated:
            extras["next_offset"] = offset + len(chunk)
        if not chunk:
            return SegmentResult(text="", error="empty result", extras=extras)
        return SegmentResult(text=chunk, extras=extras)

    async def _answer_with_vlm(
        self,
        *,
        file_row: Any,
        question: str,
        storage: StorageBackend,
    ) -> SegmentResult:
        if not has_vision_profile():
            return SegmentResult(error=(
                "image read with `question` requires the `vision` LLM "
                "profile; configure it or omit `question` to fall back "
                "to the persisted description"
            ), extras={"kind": "image"})
        try:
            body = await self._read_bytes(storage, file_row.storage_key)
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"image read failed: {exc}",
                                 extras={"kind": "image"})
        scaled, media_type = downscale_for_vlm(body)
        b64 = base64.b64encode(scaled).decode("ascii")
        client = get_chat_client("vision")
        try:
            resp = await client.complete(ChatRequest(
                system=(
                    "You are looking at one image and answering the user's "
                    "specific question about it. Be concise, ground every "
                    "claim in what is visible. If the question can't be "
                    "answered from the image alone, say so plainly."
                ),
                messages=[ChatMessage(role="user", content=[
                    TextBlock(text=f"Question: {question}"),
                    ImageBlock(media_type=media_type, data_b64=b64),
                ])],
                max_tokens=1024,
                temperature=0.2,
            ))
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"VLM call failed: {exc}",
                                 extras={"kind": "image"})
        text = (resp.text or "").strip()
        return SegmentResult(
            text=text or "(VLM returned empty response)",
            extras={
                "kind": "image",
                "vlm_used": True,
                "question": question,
                "bytes": len(body),
                "scaled_bytes": len(scaled),
            },
        )

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first variant — used by ArchivePipeline when the agent
        drills into an image member that has no persisted description.
        Down-scales and asks the VLM for a short caption (1-2 sentences),
        cheaper than re-running the full ingest schema.

        Archive ingest itself does NOT call this — see ArchivePipeline,
        which substitutes a structural placeholder for image members so
        archives don't blow up VLM cost.
        """
        scaled, media_type = downscale_for_vlm(body)
        b64 = base64.b64encode(scaled).decode("ascii")
        client = get_chat_client("vision")
        prompt = (
            f"Describe the image below in 1-2 sentences. "
            f"Filename: {filename or 'unknown'}."
        )
        try:
            resp = await client.complete(ChatRequest(
                system="You describe images concisely. Output one short paragraph, no preamble.",
                messages=[ChatMessage(role="user", content=[
                    TextBlock(text=prompt),
                    ImageBlock(media_type=media_type, data_b64=b64),
                ])],
                max_tokens=300,
                temperature=0.2,
            ))
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(
                error=f"VLM describe failed: {exc}",
                extras={"kind": "image", "filename": filename, "bytes": len(body)},
            )
        text = (resp.text or "").strip()
        return SegmentResult(
            text=text or f"[image: {filename or 'unknown'}, ~{max(1, len(body) // 1024)} KB]",
            extras={
                "kind": "image",
                "filename": filename,
                "bytes": len(body),
                "scaled_bytes": len(scaled),
            },
        )


def _render_image_description(file_row: Any) -> str:
    """Format image description into agent-readable text."""
    summary = (getattr(file_row, "summary", None) or "").strip()
    desc = getattr(file_row, "description", None) or {}
    parts: list[str] = []
    if summary:
        parts.append(summary)
    if isinstance(desc, dict):
        regions = desc.get("regions") or []
        if isinstance(regions, list) and regions:
            parts.append("")
            parts.append("Regions:")
            for i, r in enumerate(regions, start=1):
                if not isinstance(r, dict):
                    continue
                title = (r.get("title") or "").strip() or f"region {i}"
                detail = (r.get("description") or r.get("summary") or "").strip()
                parts.append(f"  [{i}] {title}: {detail}")
    return "\n".join(parts).strip()
