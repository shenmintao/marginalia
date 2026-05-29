"""Text pipeline (DESIGN.md §11.3, first batch).

Handles `text/markdown`, `text/plain`, and the `.txt` / `.md` / `.rst` extensions.
Produces `description.sections` with heading-path / line-range anchors.

Single LLM call:
  inputs : indexed text coverage, folder path, sibling names, catalog sketch,
           current tag vocabulary
  outputs: tagged text response (see marginalia.llm.tagged_response).

The system prompt is large (>1024 chars) on purpose — Anthropic adapter will
auto-place a `cache_control` marker, OpenAI will auto-cache. Subsequent text
ingests reuse the cache → most input tokens are charged once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from typing import Any

from marginalia.config import get_settings, resolve_profile
from marginalia.llm import (
    ChatRequest,
    cacheable_prompt_messages,
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
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

# Single LLM prompts stay bounded, but ingest can read substantially more and
# index it in chunks before the aggregate summary call.
MAX_TEXT_BYTES = 60_000
MAX_TEXT_INDEX_BYTES = 8 * 1024 * 1024
TEXT_CHUNK_CHARS = 50_000
TEXT_SECTION_DIGEST_BYTES = 60_000
TEXT_INDEX_MIN_OUTPUT_TOKENS = 8192
TEXT_INDEX_MAX_OUTPUT_TOKENS = 16384

# read_segment limits — we read more than the LLM-indexing path because the
# agent might want late chunks of a long file.
READ_SEGMENT_BYTES_CAP = 32 * 1024 * 1024  # 32 MB
READ_SEGMENT_DEEP_BYTES_CAP = 128 * 1024 * 1024  # 128 MB
DEFAULT_MAX_CHARS = 8000

TEXT_PIPELINE_SYSTEM = """You are Marginalia's text-document indexer.

Your job: read the indexed text provided for a single document and produce a
structured index that lets a downstream agent decide whether to retrieve it,
and once retrieved, jump to the relevant section by anchor. The indexed text
may be only the first `indexed_bytes` of a larger file; use only the content
provided and do not infer missing later content.

`summary` is one or two sentences (<=60 Chinese characters / <=30 English words) in the
document's own language — the spine of what the document is and why a
reader would open it. Keep it tight; depth belongs in `description`.
`description` is a free-text walk-through of what the document covers and how
it is organised — multi-paragraph if useful. `sections` lists every meaningful
heading or logical chunk; each line takes the form
`id | <heading-path or lines X-Y> | title | one-or-two-sentence summary |
term1, term2, term3`. The anchor is either a heading path like `1.2.3` or a
line range like `lines 100-160`. `extra` carries cross-cutting machine-readable
insights as `key: value` lines (one per line; leave the block empty if nothing
notable). `entry_extra` is the same shape but for position-aware insights.
`entry_catalog_path` is a best-guess classification path. Reuse names from the
current vocabulary when they fit; coin new ones only when nothing fits. `tags`
are 3-10 facet:name pairs; valid facets are topic | form | time | source |
language | extra.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="heading or lines", anchor_example="1.2.3 or lines 100-160",
)


TEXT_CHUNK_SYSTEM = """You are Marginalia's text section indexer.

You receive one line range from a larger text document. Produce a local index
for this range only. Use line-range anchors from the provided context, not
byte offsets. `sections` is required and should cover every meaningful heading
or logical chunk in this range.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="lines", anchor_example="lines 1200-1450",
)


TEXT_AGGREGATE_SYSTEM = """You are Marginalia's aggregate text indexer.

You receive a precomputed section map for the indexed portion of a text
document. Do NOT read or invent outside that map. If
`coverage.indexed_partial` is true, make the limited coverage clear and do not
imply that missing bytes were reviewed. Produce only file-level fields:
summary, description, extra, entry_extra, catalog_path, and tags. Do not output
a sections block; the caller will preserve the section map separately in
`description.sections`.

Make `extra` retrieval-friendly: include important alternate names, recurring
technical terms, and high-value line ranges from the section map.

""" + render_format_hint()


# Schema kept for legacy callers but no longer fed to the LLM.
TEXT_PIPELINE_SCHEMA: dict[str, Any] = {}


def _index_output_tokens(char_count: int) -> int:
    return min(
        TEXT_INDEX_MAX_OUTPUT_TOKENS,
        max(TEXT_INDEX_MIN_OUTPUT_TOKENS, char_count // 8),
    )


def _should_retry_empty_index(resp: Any, max_tokens: int) -> bool:
    if max_tokens >= TEXT_INDEX_MAX_OUTPUT_TOKENS:
        return False
    return not (getattr(resp, "text", None) or "").strip() or (
        getattr(resp, "stop_reason", None) == "max_tokens"
    )


def _response_diag(resp: Any) -> str:
    usage = getattr(resp, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    return (
        f"stop_reason={getattr(resp, 'stop_reason', None) or 'unknown'}, "
        f"input_tokens={input_tokens}, output_tokens={output_tokens}"
    )


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
        body, indexed_bytes, read_truncated = await self._read_text_with_meta(
            storage, ctx.storage_key, cap=MAX_TEXT_INDEX_BYTES,
        )
        if len(body) > MAX_TEXT_BYTES:
            return await self._run_chunked_index(
                ctx=ctx,
                body=body,
                total_bytes=ctx.size_bytes,
                indexed_bytes=indexed_bytes,
                read_truncated=read_truncated,
            )
        return await self._run_single_index(
            ctx=ctx,
            body=body,
            total_bytes=ctx.size_bytes,
            indexed_bytes=indexed_bytes,
            read_truncated=read_truncated,
        )

    async def _run_single_index(
        self,
        *,
        ctx: PipelineContext,
        body: str,
        total_bytes: int,
        indexed_bytes: int,
        read_truncated: bool,
    ) -> PipelineResult:
        user_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "indexed_bytes": indexed_bytes,
            "total_bytes": total_bytes,
        }
        stable_prefix = (
            "Index the document text below. Hints are advisory; the provided "
            "content takes precedence. If indexed_bytes is less than "
            "total_bytes, cover only the provided text and do not infer "
            "missing later content. Prefer line-range anchors when possible."
            "\n\n"
            + render_format_hint() + "\n"
            + render_sections_hint(
                anchor_unit="lines",
                anchor_example="lines 100-160",
            )
        )
        file_content = (
            f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body}\n</document>"
        )

        client = get_chat_client("ingest")
        request = ChatRequest(
            system=TEXT_PIPELINE_SYSTEM,
            messages=cacheable_prompt_messages(stable_prefix, file_content),
            max_tokens=_index_output_tokens(len(body)),
            temperature=0.2,
            cache_breakpoints=[0],
        )
        resp = await client.complete(request)
        fields = parse_index_response(resp, anchor_unit="lines")
        if not fields.summary and _should_retry_empty_index(resp, request.max_tokens):
            log.warning(
                "text pipeline: empty index response; retrying with larger "
                "output budget (%s)",
                _response_diag(resp),
            )
            request = replace(request, max_tokens=TEXT_INDEX_MAX_OUTPUT_TOKENS)
            resp = await client.complete(request)
            fields = parse_index_response(resp, anchor_unit="lines")
        if not fields.summary:
            log.warning(
                "text pipeline: no <summary> in response (%s). text=%r",
                _response_diag(resp),
                (resp.text or "")[:300],
            )
            raise ValueError(
                "text pipeline produced empty summary "
                f"({_response_diag(resp)})"
            )
        total_lines = max(1, len(body.splitlines()))
        sections = fields.sections or [
            fallback_section(
                title="Document",
                anchor_unit="lines",
                anchor_value=f"1-{total_lines}",
                summary=fields.summary,
            )
        ]
        coverage = self._coverage(
            total_bytes=total_bytes,
            indexed_bytes=indexed_bytes,
            chunk_count=1,
            read_truncated=read_truncated,
        )
        return self._result_from_fields(
            fields=fields,
            sections=renumber_sections(sections),
            coverage=coverage,
        )

    async def _run_chunked_index(
        self,
        *,
        ctx: PipelineContext,
        body: str,
        total_bytes: int,
        indexed_bytes: int,
        read_truncated: bool,
    ) -> PipelineResult:
        client = get_chat_client("ingest")
        sections: list[dict[str, Any]] = []
        chunk_summaries: list[dict[str, Any]] = []
        chunks = list(enumerate(
            _iter_line_chunks(body, max_chars=TEXT_CHUNK_CHARS),
            start=1,
        ))
        sem = asyncio.Semaphore(llm_ingest_concurrency())

        async def _index_chunk(
            chunk_no: int,
            line_start: int,
            line_end: int,
            text: str,
        ) -> dict[str, Any]:
            async with sem:
                payload = {
                    "folder_path": ctx.folder_path,
                    "sibling_names": ctx.sibling_names,
                    "catalog_sketch": ctx.catalog_sketch,
                    "tag_vocabulary": ctx.tag_vocabulary,
                    "line_start": line_start,
                    "line_end": line_end,
                    "chunk_no": chunk_no,
                    "indexed_bytes": indexed_bytes,
                    "total_bytes": total_bytes,
                }
                stable_prefix = (
                    "Index this line range from a larger text document. Use "
                    "line-range anchors.\n\n"
                    + render_format_hint() + "\n"
                    + render_sections_hint(
                        anchor_unit="lines",
                        anchor_example=f"lines {line_start}-{line_end}",
                    )
                )
                file_content = (
                    f"<context>\n{json.dumps(payload, ensure_ascii=False)}\n</context>\n\n"
                    f"<document>\n{text}\n</document>"
                )
                request = ChatRequest(
                    system=TEXT_CHUNK_SYSTEM,
                    messages=cacheable_prompt_messages(stable_prefix, file_content),
                    max_tokens=_index_output_tokens(len(text)),
                    temperature=0.2,
                    cache_breakpoints=[0],
                )
                resp = await client.complete(request)
                fields = parse_index_response(resp, anchor_unit="lines")
                if not fields.summary and _should_retry_empty_index(
                    resp,
                    request.max_tokens,
                ):
                    log.warning(
                        "text chunk pipeline: empty index response for lines "
                        "%s-%s; retrying with larger output budget (%s)",
                        line_start,
                        line_end,
                        _response_diag(resp),
                    )
                    retry_request = replace(
                        request,
                        max_tokens=TEXT_INDEX_MAX_OUTPUT_TOKENS,
                    )
                    resp = await client.complete(retry_request)
                    fields = parse_index_response(resp, anchor_unit="lines")
            summary = fields.summary or fields.description_text or f"Lines {line_start}-{line_end}"
            local_sections = fields.sections or [
                fallback_section(
                    title=f"Lines {line_start}-{line_end}",
                    anchor_unit="lines",
                    anchor_value=f"{line_start}-{line_end}",
                    summary=summary,
                )
            ]
            return {
                "sections": local_sections,
                "summary": {
                    "line_start": line_start,
                    "line_end": line_end,
                    "summary": summary,
                    "description": fields.description_text or "",
                },
            }

        chunk_results = await asyncio.gather(*(
            _index_chunk(chunk_no, line_start, line_end, text)
            for chunk_no, (line_start, line_end, text) in chunks
        ))
        for result in chunk_results:
            sections.extend(result["sections"])
            chunk_summaries.append(result["summary"])

        sections = renumber_sections(sections)
        coverage = self._coverage(
            total_bytes=total_bytes,
            indexed_bytes=indexed_bytes,
            chunk_count=len(chunk_summaries),
            read_truncated=read_truncated,
        )
        aggregate_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "coverage": coverage,
            "chunk_summaries": chunk_summaries,
        }
        aggregate_content = (
            f"<context>\n{json.dumps(aggregate_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<section_map>\n{render_sections_digest(sections, max_chars=TEXT_SECTION_DIGEST_BYTES)}\n</section_map>"
        )
        request = ChatRequest(
            system=TEXT_AGGREGATE_SYSTEM,
            messages=cacheable_prompt_messages(
                (
                    "Summarize the indexed text coverage from this section map. "
                    "The caller already has `description.sections`; produce "
                    "file-level recall fields only."
                ),
                aggregate_content,
            ),
            max_tokens=8192,
            temperature=0.2,
            cache_breakpoints=[0],
        )
        resp = await client.complete(request)
        fields = parse_index_response(resp, anchor_unit="lines")
        if not fields.summary and _should_retry_empty_index(resp, request.max_tokens):
            log.warning(
                "text aggregate pipeline: empty index response; retrying "
                "with larger output budget (%s)",
                _response_diag(resp),
            )
            retry_request = replace(
                request,
                max_tokens=TEXT_INDEX_MAX_OUTPUT_TOKENS,
            )
            resp = await client.complete(retry_request)
            fields = parse_index_response(resp, anchor_unit="lines")
        if not fields.summary:
            first = chunk_summaries[0]["summary"] if chunk_summaries else "text document"
            fields.summary = (
                f"Long text indexed into {len(chunk_summaries)} line ranges. "
                f"First range: {first}"
            )
        return self._result_from_fields(
            fields=fields,
            sections=sections,
            coverage=coverage,
        )

    def _result_from_fields(
        self,
        *,
        fields,
        sections: list[dict[str, Any]],
        coverage: dict[str, Any],
    ) -> PipelineResult:
        description: dict[str, Any] = {
            "sections": sections,
            "coverage": coverage,
        }
        if fields.description_text:
            description["text"] = fields.description_text
        return PipelineResult(
            summary=fields.summary,
            description=description,
            kind="text",
            extra=build_retrieval_extra(
                sections=sections,
                coverage=coverage,
                base_extra=fields.extra,
            ),
            entry_extra=fields.entry_extra,
            entry_catalog_path=fields.catalog_path,
            entry_tags=fields.tags,
        )

    @staticmethod
    def _coverage(
        *,
        total_bytes: int,
        indexed_bytes: int,
        chunk_count: int,
        read_truncated: bool,
    ) -> dict[str, Any]:
        indexed_partial = read_truncated or indexed_bytes < total_bytes
        partial_reasons: list[str] = []
        if indexed_partial:
            partial_reasons.append("text_index_byte_cap")
        return {
            "unit": "bytes",
            "source_mode": "text_extracted_bytes",
            "total_units": total_bytes,
            "indexed_units": indexed_bytes,
            "total_bytes": total_bytes,
            "indexed_bytes": indexed_bytes,
            "indexed_partial": indexed_partial,
            "partial_reasons": partial_reasons,
            "max_index_bytes": MAX_TEXT_INDEX_BYTES,
            "chunked": chunk_count > 1,
            "chunk_count": chunk_count,
            "text_truncated": read_truncated,
        }

    # ---- read_segment -----------------------------------------------------

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        body, n_read, source_truncated = await self._read_text_with_meta(
            storage, file_row.storage_key,
            cap=_read_cap_for_args(args, file_row=file_row),
        )
        seg = self._slice(
            body=body, args=args, file_row=file_row,
        )
        seg.extras.setdefault("source_bytes_read", n_read)
        if source_truncated:
            seg.extras["source_truncated"] = True
            seg.extras["hint"] = (
                "The file is longer than this read window; use offset, "
                "line_start/line_end, section_id, heading, or pattern to drill in."
            )
        return seg

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
          1. pattern    → regex search with context_lines / max_matches /
                          match_offset; restricted to the line_start..
                          line_end window when those are also passed
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
            scope_body = body
            scope_line_offset = 0
            ls_raw = args.get("line_start")
            le_raw = args.get("line_end")
            if ls_raw or le_raw:
                try:
                    ls = max(1, int(ls_raw)) if ls_raw else 1
                    le = int(le_raw) if le_raw else None
                except (TypeError, ValueError):
                    return SegmentResult(error="line_start/line_end must be integers")
                lines_all = body.splitlines()
                if le is None:
                    le = len(lines_all)
                if le < ls:
                    return SegmentResult(error="line_end must be >= line_start")
                scope_body = "\n".join(lines_all[ls - 1: le])
                scope_line_offset = ls - 1
            return _pattern_search(
                body=scope_body, pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                line_offset=scope_line_offset,
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
        # Compute line range from char offset so footnotes can deep-link
        # even when the LLM reads by offset rather than line_start/line_end.
        line_start = body[:offset].count("\n") + 1
        line_end = line_start + chunk.count("\n")
        extras = {
            "offset": offset,
            "char_count": len(chunk),
            "total_chars": total,
            "truncated": truncated,
            "next_offset": offset + len(chunk) if truncated else None,
            "line_start": line_start,
            "line_end": line_end,
        }
        return SegmentResult(text=chunk, extras=extras)

    @staticmethod
    async def _read_text(
        storage: StorageBackend, key: str, cap: int = MAX_TEXT_BYTES,
    ) -> str:
        text, _n, _truncated = await TextPipeline._read_text_with_meta(
            storage, key, cap=cap,
        )
        return text

    @staticmethod
    async def _read_text_with_meta(
        storage: StorageBackend, key: str, cap: int,
    ) -> tuple[str, int, bool]:
        buf = bytearray()
        truncated = False
        async for chunk in storage.get(key):
            buf.extend(chunk)
            if len(buf) > cap:
                buf = bytearray(buf[:cap])
                truncated = True
                break
        return _decode_text(bytes(buf)), len(buf), truncated


# ---- read_segment helpers ----------------------------------------------------

def _iter_line_chunks(
    body: str, *, max_chars: int,
) -> list[tuple[int, int, str]]:
    lines = body.splitlines()
    if not lines:
        return [(1, 1, "")]
    chunks: list[tuple[int, int, str]] = []
    cur: list[str] = []
    cur_start = 1
    cur_len = 0
    for idx, line in enumerate(lines, start=1):
        line_cost = len(line) + 1
        if cur and cur_len + line_cost > max_chars:
            chunks.append((cur_start, idx - 1, "\n".join(cur)))
            cur = []
            cur_start = idx
            cur_len = 0
        cur.append(line)
        cur_len += line_cost
    if cur:
        chunks.append((cur_start, cur_start + len(cur) - 1, "\n".join(cur)))
    return chunks


def _read_cap_for_args(args: dict[str, Any], *, file_row: Any) -> int:
    wants_deep_read = any(
        args.get(k) for k in ("section_id", "heading", "line_start", "line_end", "pattern")
    )
    cap = READ_SEGMENT_DEEP_BYTES_CAP if wants_deep_read else 0

    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    if max_chars <= 0:
        max_chars = DEFAULT_MAX_CHARS

    if wants_deep_read:
        # Deep reads may need to scan for a section/heading/pattern, but still
        # include enough bytes for an explicit late offset if one is supplied.
        cap = max(cap, (offset + max_chars + 4096) * 4)
    else:
        # Default chunk reads should stay proportional to the requested
        # window, but late offsets must still be reachable. UTF-8 can take
        # up to four bytes per char.
        cap = (offset + max_chars + 4096) * 4

    raw_size = getattr(file_row, "size_bytes", None)
    try:
        size = int(raw_size)
    except (TypeError, ValueError):
        size = 0
    if size > 0:
        cap = min(cap, size)
    return cap


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
    match_offset: int = 0, line_offset: int = 0,
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
        for i, start in enumerate(line_starts):
            if start > pos:
                return i  # 1-indexed
        return len(lines)

    all_matches = list(rx.finditer(body))
    total = len(all_matches)
    sliced = all_matches[match_offset: match_offset + max_matches]
    hits: list[dict[str, Any]] = []
    for m in sliced:
        line_no = line_of(m.start())
        s = max(0, line_no - 1 - context_lines)
        e = min(len(lines), line_no + context_lines)
        hits.append({
            "line": line_no + line_offset,
            "match": m.group(0)[:200],
            "context": "\n".join(lines[s:e]),
        })

    has_more = (match_offset + len(hits)) < total
    extras: dict[str, Any] = {
        "pattern": pattern,
        "match_count": len(hits),
        "total_matches": total,
        "match_offset": match_offset,
        "has_more": has_more,
        "hits": hits,
    }
    if has_more:
        extras["next_match_offset"] = match_offset + len(hits)

    if not hits:
        if match_offset and total:
            err = (
                f"match_offset {match_offset} exceeds total_matches {total}"
            )
        else:
            err = "no matches"
        return SegmentResult(text="", error=err, extras=extras)

    rendered = "\n\n".join(
        f"[L{h['line']}] {h['match']}\n  ┊ {h['context']}"
        for h in hits
    )
    return SegmentResult(text=rendered, extras=extras)


def _decode_text(buf: bytes) -> str:
    """Robust decode — text mime says "should be utf-8" but we tolerate
    BOM / utf-16 / arbitrary as last resort."""
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return buf.decode(enc)
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace")
