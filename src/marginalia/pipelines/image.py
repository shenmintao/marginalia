"""Image pipeline (design.md §11.3).

Handles raster images: image/png / image/jpeg / image/gif / image/webp.
Uses the `vision` LLM profile (a multimodal model) and feeds the image
bytes as a base64 ImageBlock — the abstraction layer translates to each
provider's native shape (OpenAI: data: URL; Anthropic: base64 source).

Single LLM call producing structured JSON: a description of the image's
content, key regions / objects, suggested catalog placement, and tags.
"""
from __future__ import annotations

import base64
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
    TagSuggestion,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB cap (most VLMs reject larger)

_MIME_TO_LITERAL = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}


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
        media_type = _MIME_TO_LITERAL.get(
            (ctx.mime_type or "").lower(), "image/png",
        )
        b64 = base64.b64encode(body).decode("ascii")

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
