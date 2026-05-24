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

from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    get_chat_client,
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
downstream agent decide whether to retrieve it, and once retrieved, find
the relevant region.

Rules:
- Output ONLY one JSON object matching the provided schema. No prose, no fences.
- `summary`: 2-4 sentences in the user's likely language describing what
  the image shows.
- `description.regions`: an array of meaningful regions / objects / panels.
  Each region: a stable id (r1, r2, …), a short label (the visible text or
  inferred caption), a brief summary, and 3-7 key terms.
- `kind`: "image".
- `extra`: at most 1 paragraph of cross-cutting content insight (themes,
  notable patterns). Empty string if nothing notable.
- `entry_extra`: at most 1 paragraph of position-aware insight. Empty
  string if the position carries no extra signal.
- `entry_catalog_path`: best-guess classification path as a list of names.
  Use the catalog sketch as a hint, not a constraint.
- `entry_tags`: 3-10 tags. Facets are exactly:
  topic | form | time | source | language | extra.
"""


IMAGE_PIPELINE_SCHEMA: dict[str, Any] = {
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
            "required": ["regions"],
            "properties": {
                "regions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["id", "label", "summary", "key_terms"],
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "summary": {"type": "string"},
                            "key_terms": {
                                "type": "array", "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "kind": {"type": "string", "enum": ["image"]},
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
            max_tokens=2048,
            json_schema=IMAGE_PIPELINE_SCHEMA,
            temperature=0.2,
        ))

        if resp.parsed_json is None:
            log.warning(
                "image pipeline: model did not return parseable JSON. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("image pipeline produced non-JSON output")

        data = resp.parsed_json
        return PipelineResult(
            summary=str(data["summary"]),
            description={"regions": data["description"]["regions"]},
            kind="image",
            extra=(data.get("extra") or "") or None,
            entry_extra=(data.get("entry_extra") or "") or None,
            entry_catalog_path=list(data.get("entry_catalog_path") or []) or None,
            entry_tags=[
                TagSuggestion(name=str(t["name"]), facet=str(t["facet"]))
                for t in (data.get("entry_tags") or [])
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
        """Images don't have a text body. read_segment renders the
        ingested description as text — summary plus per-region notes —
        so the agent can quote from it. Generic offset/max_chars still
        apply for chunked reads of long descriptions."""
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
