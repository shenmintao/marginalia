"""Text pipeline (DESIGN.md §11.3, first batch).

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
import re
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
    SegmentResult,
    TagSuggestion,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

# Truncate very long files; we still want a holistic summary, but we keep the
# prompt bounded. 60 KB ≈ 15-20K tokens depending on language.
MAX_TEXT_BYTES = 60_000

# read_segment limits — we read more than the LLM-indexing path because the
# agent might want late chunks of a long file.
READ_SEGMENT_BYTES_CAP = 4 * 1024 * 1024  # 4 MB
DEFAULT_MAX_CHARS = 8000

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

    # ---- read_segment -----------------------------------------------------

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        body = await self._read_text(
            storage, file_row.storage_key, cap=READ_SEGMENT_BYTES_CAP,
        )
        return self._slice(
            body=body, args=args, file_row=file_row,
        )

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first variant — used by ArchivePipeline for member peeks
        and dispatched reads. No file_row, so section_id / heading lookups
        (which rely on persisted description.sections) are unavailable.
        """
        text = _decode_text(body[:READ_SEGMENT_BYTES_CAP])
        return self._slice(body=text, args=args, file_row=None)

    def _slice(
        self,
        *,
        body: str,
        args: dict[str, Any],
        file_row: Any | None,
    ) -> SegmentResult:
        """Resolve the args dict against this file's text body.

        Priority (first matching field wins):
          1. pattern    → regex search with context_lines / max_matches
          2. section_id → look up in description.sections, return its body
          3. heading    → find by section title, return its body
          4. line_start → return the line range
          5. (default)  → return the offset..offset+max_chars chunk

        offset/max_chars also act as a clamp on the result of (2)-(4).
        """
        offset = max(0, int(args.get("offset") or 0))
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
        if max_chars <= 0:
            max_chars = DEFAULT_MAX_CHARS

        pattern = (args.get("pattern") or "").strip()
        if pattern:
            return _pattern_search(
                body=body, pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
            )

        section_id = (args.get("section_id") or "").strip()
        heading = (args.get("heading") or "").strip()
        if section_id or heading:
            sections = _sections_from_file(file_row) if file_row else None
            if sections is None:
                return SegmentResult(
                    error="section_id/heading lookup needs persisted description",
                )
            target = _find_section(sections, section_id=section_id, heading=heading)
            if target is None:
                miss = section_id or f"heading={heading!r}"
                return SegmentResult(error=f"section not found: {miss}")
            text, extras = _section_body(target, body)
            return _clamp(text, offset, max_chars, extras=extras)

        line_start = args.get("line_start")
        line_end = args.get("line_end")
        if line_start:
            try:
                ls = max(1, int(line_start))
            except (TypeError, ValueError):
                return SegmentResult(error="line_start must be an integer")
            try:
                le = int(line_end) if line_end else ls
            except (TypeError, ValueError):
                return SegmentResult(error="line_end must be an integer")
            if le < ls:
                return SegmentResult(error="line_end must be >= line_start")
            lines = body.splitlines()
            sliced = lines[ls - 1: le]
            text = "\n".join(sliced)
            return _clamp(
                text, offset, max_chars,
                extras={
                    "line_start": ls, "line_end": le,
                    "line_count": len(sliced),
                    "total_lines": len(lines),
                },
            )

        # Default: chunk-read. offset..offset+max_chars of the entire body.
        total = len(body)
        chunk = body[offset: offset + max_chars]
        truncated = (offset + len(chunk)) < total
        return SegmentResult(
            text=chunk,
            extras={
                "offset": offset,
                "char_count": len(chunk),
                "total_chars": total,
                "truncated": truncated,
                "next_offset": offset + len(chunk) if truncated else None,
            },
        )

    @staticmethod
    async def _read_text(
        storage: StorageBackend, key: str, cap: int = MAX_TEXT_BYTES,
    ) -> str:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
            if len(buf) > cap:
                buf = bytearray(buf[:cap])
                break
        return _decode_text(bytes(buf))


# ---- read_segment helpers ----------------------------------------------------

def _sections_from_file(file_row: Any) -> list[dict] | None:
    desc = getattr(file_row, "description", None)
    if not isinstance(desc, dict):
        return None
    sections = desc.get("sections")
    if not isinstance(sections, list):
        return None
    return [s for s in sections if isinstance(s, dict)]


def _find_section(
    sections: list[dict], *, section_id: str = "", heading: str = "",
) -> dict | None:
    if section_id:
        for s in sections:
            if s.get("id") == section_id:
                return s
    if heading:
        for s in sections:
            if (s.get("title") or "").strip() == heading.strip():
                return s
    return None


def _section_body(section: dict, full_text: str) -> tuple[str, dict[str, Any]]:
    """Resolve a section's anchor against the full text body.

    Returns (text, extras). Falls back to the section's own summary +
    key_terms if the anchor cannot be located in the body.
    """
    anchor = section.get("anchor") or {}
    a_unit = anchor.get("unit")
    a_value = anchor.get("value")

    if a_unit == "lines" and isinstance(a_value, str) and "-" in a_value:
        try:
            start, end = (int(x) for x in a_value.split("-"))
            lines = full_text.splitlines()
            sliced = lines[max(0, start - 1): end]
            return "\n".join(sliced), {
                "title": section.get("title"),
                "section_id": section.get("id"),
                "anchor": {"unit": "lines", "value": a_value},
                "line_count": len(sliced),
            }
        except ValueError:
            pass

    title = (section.get("title") or "").strip()
    if title:
        idx = full_text.find(title)
        if idx != -1:
            # Take from heading to the next ~4KB of text (or to next heading
            # if we can spot one — kept simple here).
            return full_text[idx: idx + 4096], {
                "title": title,
                "section_id": section.get("id"),
                "located_via": "title-scan",
            }

    return "", {
        "title": section.get("title"),
        "section_id": section.get("id"),
        "summary": section.get("summary"),
        "key_terms": section.get("key_terms"),
        "note": "anchor not resolvable from body; section summary returned in extras",
    }


def _clamp(
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
    if not chunk and not extras.get("note"):
        return SegmentResult(text="", error="empty result", extras=extras)
    return SegmentResult(text=chunk, extras=extras)


def _pattern_search(
    *, body: str, pattern: str, context_lines: int, max_matches: int,
) -> SegmentResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return SegmentResult(error=f"invalid regex: {exc}")

    lines = body.splitlines()
    line_starts: list[int] = [0]
    for i, ln in enumerate(lines):
        line_starts.append(line_starts[-1] + len(ln) + 1)

    def line_of(pos: int) -> int:
        # binary search would be tighter; simple linear is fine here
        for i, start in enumerate(line_starts):
            if start > pos:
                return i  # 1-indexed
        return len(lines)

    hits: list[dict[str, Any]] = []
    for m in rx.finditer(body):
        if len(hits) >= max_matches:
            break
        line_no = line_of(m.start())
        s = max(0, line_no - 1 - context_lines)
        e = min(len(lines), line_no + context_lines)
        hits.append({
            "line": line_no,
            "match": m.group(0)[:200],
            "context": "\n".join(lines[s:e]),
        })

    if not hits:
        return SegmentResult(
            text="",
            error="no matches",
            extras={"pattern": pattern},
        )

    rendered = "\n\n".join(
        f"[L{h['line']}] {h['match']}\n  ┊ {h['context']}"
        for h in hits
    )
    return SegmentResult(
        text=rendered,
        extras={
            "pattern": pattern,
            "match_count": len(hits),
            "hits": hits,
        },
    )


def _decode_text(buf: bytes) -> str:
    """Robust decode — text mime says "should be utf-8" but we tolerate
    BOM / utf-16 / arbitrary as last resort."""
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return buf.decode(enc)
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace")
