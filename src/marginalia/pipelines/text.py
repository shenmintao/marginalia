"""Text pipeline (design.md §11.3, first batch).

Handles `text/markdown`, `text/plain`, and the `.txt` / `.md` / `.rst` extensions.
Produces `description.sections` with heading-path / line-range anchors.

Single LLM call:
  inputs : full text (truncated if huge), folder path, sibling names, catalog
           sketch, current tag vocabulary
  outputs: structured JSON matching TEXT_PIPELINE_SCHEMA

The system prompt is large (>1024 chars) on purpose — Anthropic adapter will
auto-place a `cache_control` marker, OpenAI will auto-cache. Subsequent text
ingests reuse the cache → most input tokens are charged once.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from marginalia.config import get_settings, resolve_profile
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

# Truncate very long files; we still want a holistic summary, but we keep the
# prompt bounded. 60 KB ≈ 15-20K tokens depending on language.
MAX_TEXT_BYTES = 60_000

TEXT_PIPELINE_SYSTEM = """You are Marginalia's text-document indexer.

Your job: read a single text document and produce a structured index that lets
a downstream agent decide whether to retrieve it, and once retrieved, jump to
the relevant section by anchor.

Rules:
- Output ONLY one JSON object matching the provided schema. No prose, no fences.
- `summary`: 2-4 sentences in the document's own language, content-focused.
- `description.sections`: array of every meaningful heading or logical chunk.
  For each section: a stable id (s1, s2, …), the heading title, an anchor
  (`unit`: "heading" with `path` like "1.2.3", or "lines" with [start,end]),
  a 1-2 sentence summary, and 3-7 key terms.
- `kind`: "text".
- `extra`: at most 1 paragraph of cross-cutting content insight (themes,
  notable patterns). Empty string if nothing notable.
- `entry_extra`: at most 1 paragraph of position-aware insight, e.g. how this
  document relates to its sibling files in the same folder. Empty string if
  the position carries no extra signal.
- `entry_catalog_path`: best-guess classification path as a list of names,
  rooted at a top-level catalog (e.g. ["Research","LLM","Reasoning"]). The
  current catalog sketch is a hint, not a constraint — propose a new path if
  needed.
- `entry_tags`: 3-10 tags. Each `{name, facet}`. Facets are exactly:
  topic | form | time | source | language | extra. Reuse names from the
  current vocabulary when they fit; coin new ones only when nothing fits.
"""

TEXT_PIPELINE_SCHEMA: dict[str, Any] = {
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
                                    "unit": {"type": "string", "enum": ["heading", "lines"]},
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
                        "enum": ["topic", "form", "time", "source", "language", "extra"],
                    },
                },
            },
        },
    },
}


@register_pipeline(
    mimes=("text/plain", "text/markdown", "text/x-rst"),
    mime_prefixes=("text/",),
    exts=(".txt", ".md", ".markdown", ".rst"),
)
class TextPipeline(Pipeline):
    name = "text"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        body = await self._read_text(storage, ctx.storage_key)

        user_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "document": body,
        }
        user_text = (
            "Index the document below. Hints are advisory — the document's "
            "actual content takes precedence.\n\n"
            f"<context>\n{json.dumps({k: v for k, v in user_payload.items() if k != 'document'}, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body}\n</document>"
        )

        client = get_chat_client("ingest")
        # Determine an output token ceiling based on document size — small
        # docs need a small ceiling, larger docs proportionally more.
        max_out = min(4096, max(1024, len(body) // 10))

        resp = await client.complete(ChatRequest(
            system=TEXT_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
            max_tokens=max_out,
            json_schema=TEXT_PIPELINE_SCHEMA,
            temperature=0.2,
        ))

        if resp.parsed_json is None:
            log.warning(
                "text pipeline: model did not return parseable JSON. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("text pipeline produced non-JSON output")

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
    async def _read_text(storage: StorageBackend, key: str) -> str:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
            if len(buf) > MAX_TEXT_BYTES:
                buf = bytearray(buf[:MAX_TEXT_BYTES])
                break
        # robust decode — text mime says "should be utf-8" but we tolerate
        # latin-1 / arbitrary as last resort.
        for enc in ("utf-8", "utf-8-sig", "utf-16"):
            try:
                return buf.decode(enc)
            except UnicodeDecodeError:
                continue
        return buf.decode("utf-8", errors="replace")
