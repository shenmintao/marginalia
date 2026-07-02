"""read_files — agent tool, dispatcher over Pipeline.read_segment.

Accepts a list of `requests`, each:

  {
    "entry_id": "...",
    "reads": [
      // any of the args fields a pipeline understands
      { "offset": 0, "max_chars": 8000 },
      { "section_id": "s3" },
      { "heading": "Algorithm" },
      { "line_start": 100, "line_end": 150 },
      { "page_start": 3, "page_end": 5 },          // PDF only
      { "page_label": "54" },                      // PDF printed label
      { "paragraph_start": 4, "paragraph_end": 12 }, // DOCX only
      { "slide_start": 2, "slide_end": 4 },          // PPTX only
      { "pattern": "leader.*election", "context_lines": 3, "max_matches": 10 },
      { "member_path": "papers/raft.pdf", "page_start": 4 }   // container
    ]
  }

Each `reads` entry is an args dict handed to `pipeline.read_segment`.
Pipelines pick the fields they understand and ignore the rest. If a
read item has fields the pipeline doesn't support, it returns
`error="..."` and the caller's `ok` is False for that item.

Same-entry requests share one DB lookup but each `reads` item triggers
a fresh storage round-trip — pipelines are responsible for their own
caching if desired.

`entry_id` may be a full uuid OR an unambiguous short prefix (>= 8 hex
chars). The model often sees 8-char prefixes in the activity bar and
parrots them back; resolve_entry_prefix promotes them to a full uuid
when there's exactly one match, and surfaces an ambiguity error when
multiple entries share the prefix.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.read_compression import (
    CompressionSettings,
    compress_read_text,
)
from marginalia.agent.tools import ToolContext, tool
from marginalia.config import get_settings
from marginalia.db.models import File, FileEntry
from marginalia.pipelines.base import Pipeline, SegmentResult
from marginalia.pipelines.registry import resolve_pipeline
from marginalia.repositories import entries as entries_repo
from marginalia.storage import get_storage

MAX_EFFECTIVE_READS_PER_ENTRY = 50

_LOCATOR_VALUE_KEYS = {
    "heading",
    "pattern",
    "member_path",
    "section_id",
    "page_label",
    "question",
    "image_id",
}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requests"],
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id"],
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "Entry UUID (or short hex prefix, ≥ 8 chars). "
                            "NOT a file name or display_name — get it from "
                            "search_metadata / list_folder first."
                        ),
                    },
                    "reads": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                # generic chunking
                                "offset": {"type": "integer", "minimum": 0},
                                "max_chars": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 16000,
                                },
                                "compress": {
                                    "type": "boolean",
                                    "description": (
                                        "Set false to reopen an omitted "
                                        "compressed region exactly, without "
                                        "read_files result compression."
                                    ),
                                },
                                # text-shaped
                                "line_start": {"type": "integer", "minimum": 1},
                                "line_end": {"type": "integer", "minimum": 1},
                                "section_id": {"type": "string"},
                                "heading": {"type": "string"},
                                # PDF
                                "page_start": {"type": "integer", "minimum": 1},
                                "page_end": {"type": "integer", "minimum": 1},
                                "page_label": {
                                    "type": "string",
                                    "description": (
                                        "PDF only: printed/logical page label "
                                        "from the PDF's page-label metadata. "
                                        "Prefer page_start/page_end from prior "
                                        "read_files results when available."
                                    ),
                                },
                                # DOCX
                                "paragraph_start": {"type": "integer", "minimum": 1},
                                "paragraph_end": {"type": "integer", "minimum": 1},
                                # PPTX
                                "slide_start": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "PPTX only: first 1-indexed slide to read."
                                    ),
                                },
                                "slide_end": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "PPTX only: last 1-indexed slide to read."
                                    ),
                                },
                                # Embedded images in document-shaped files
                                "image_id": {
                                    "type": "string",
                                    "description": (
                                        "DOCX/PPTX image question mode: exact "
                                        "embedded image id from a prior read, "
                                        "such as docx-img-1 or pptx-img-2."
                                    ),
                                },
                                "image_index": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "DOCX/PPTX image question mode: "
                                        "1-indexed embedded image number."
                                    ),
                                },
                                # pattern search
                                "pattern": {"type": "string"},
                                "patterns": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 10,
                                    "description": (
                                        "Multiple patterns to search in the same scope. "
                                        "The tool expands this into one pattern read per "
                                        "term."
                                    ),
                                },
                                "context_lines": {"type": "integer", "minimum": 0},
                                "max_matches": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 100,
                                },
                                "match_offset": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": (
                                        "Skip first N pattern hits — "
                                        "page through matches with "
                                        "next_match_offset from prior call."
                                    ),
                                },
                                # container
                                "member_path": {"type": "string"},
                                # VLM-on-read: required for image entries.
                                # Optional for OCR-indexed PDFs and for
                                # DOCX/PPTX embedded images. Without it,
                                # the pipeline reads stored extracted text;
                                # with it, the pipeline sends scoped pixels
                                # to the vision model.
                                "question": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "REQUIRED for image entries. Optional "
                                        "for OCR-indexed PDFs and DOCX/PPTX "
                                        "embedded images: omit it to read "
                                        "stored extracted text, or include it "
                                        "to send scoped pixels to the vision "
                                        "model. Combine with `page_start`/"
                                        "`page_end`, `slide_start`/`slide_end`, "
                                        "`paragraph_start`/`paragraph_end`, "
                                        "`image_id`, or `image_index` to scope "
                                        "the question."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@tool(
    name="read_files",
    description=(
        "Open file contents for one or more entries. Each request lists "
        "one or more `reads`, each addressing a slice via fields the "
        "pipeline understands (offset/max_chars for any file; "
        "page_start/page_end or page_label for PDF; "
        "slide_start/slide_end for PPTX; "
        "line_start/line_end / section_id / "
        "heading for text; pattern or patterns for regex search; member_path for "
        "container members). `pattern` can be combined with a range "
        "(page_start/end, line_start/end, paragraph_start/end, or "
        "spreadsheet heading) to restrict the search to that window — "
        "useful for a long file where the same term appears many times. "
        "PPTX `pattern` can be combined with slide_start/slide_end. "
        "Pattern hits are paginated via `match_offset` (use the "
        "`next_match_offset` from a previous response). Pass `question` "
        "for image entries, OCR-indexed PDFs, or DOCX/PPTX embedded images. "
        "For OCR-indexed PDFs, omit it to read stored OCR text, or pass it "
        "to send rendered pages to the vision model for a targeted re-check. "
        "Use AFTER read_entries_metadata identified relevant sections."
    ),
    schema=SCHEMA,
)
async def read_files(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    requests = list(args.get("requests") or [])
    if not requests:
        return {"ok": True, "results": [], "count": 0}

    # Resolve each entry_id to a full uuid. Accept full ids and short
    # hex prefixes (>= 8 chars). Ambiguous or unknown prefixes get a
    # per-entry error result so the agent stops looping on a fabricated
    # / parroted short id.
    resolved: list[tuple[Mapping[str, Any], str]] = []
    early_results: list[dict[str, Any]] = []
    for req in requests:
        raw = (req.get("entry_id") or "").strip() if isinstance(req, dict) else ""
        if not raw:
            early_results.append({
                "ok": False, "entry_id": "", "error": "missing entry_id",
            })
            continue
        full, err = await entries_repo.resolve_entry_id_prefix(db, raw)
        if err:
            early_results.append({"ok": False, "entry_id": raw, "error": err})
            continue
        resolved.append((req, full))

    entry_ids = [eid for _, eid in resolved]
    rows = await entries_repo.list_with_file_by_ids_any(db, entry_ids)
    by_entry: dict[str, tuple[FileEntry, File]] = {
        e.id: (e, f) for e, f in rows
    }

    storage = get_storage()
    settings = get_settings()
    compression_settings = CompressionSettings(
        enabled=settings.compression_enabled,
        min_chars=settings.compression_min_chars,
        target_chars=settings.compression_target_chars,
        context_chars=settings.compression_context_chars,
        max_ratio=settings.compression_max_ratio,
    )
    results: list[dict[str, Any]] = list(early_results)

    for req, eid in resolved:
        pair = by_entry.get(eid)
        if pair is None:
            results.append({
                "ok": False, "entry_id": eid, "error": "entry not found",
            })
            continue
        entry, file_row = pair

        if file_row.ingest_status != "done":
            results.append({
                "ok": False, "entry_id": eid,
                "display_name": entry.display_name,
                "error": (
                    f"ingest_status={file_row.ingest_status}; "
                    "file is not yet readable"
                ),
            })
            continue

        pipeline = resolve_pipeline(
            file_row.mime_type, file_row.original_ext,
            filename=entry.display_name,
        )
        if pipeline is None:
            results.append({
                "ok": False, "entry_id": eid,
                "display_name": entry.display_name,
                "error": (
                    f"no pipeline registered for mime={file_row.mime_type} "
                    f"ext={file_row.original_ext}"
                ),
            })
            continue

        reads_args = list(req.get("reads") or [{}])  # default: full chunk-read
        result_obj: dict[str, Any] = {
            "ok": True,
            "entry_id": eid,
            "display_name": entry.display_name,
            "kind": file_row.kind,
            "pipeline": pipeline.name,
            "reads": [],
        }
        any_failed = False
        expanded_reads = _expand_reads(reads_args)
        if len(expanded_reads) > MAX_EFFECTIVE_READS_PER_ENTRY:
            result_obj["ok"] = False
            result_obj["reads"].append({
                "ok": False,
                "error": (
                    f"too many effective reads after pattern expansion "
                    f"({len(expanded_reads)} > {MAX_EFFECTIVE_READS_PER_ENTRY})"
                ),
            })
            results.append(result_obj)
            continue

        for read_args in expanded_reads:
            seg = await _safe_read_segment(
                pipeline=pipeline, file_row=file_row,
                args=dict(read_args), storage=storage,
            )
            entry_dict: dict[str, Any] = {
                "ok": seg.error is None,
                "args": read_args,
            }
            if seg.error:
                entry_dict["error"] = seg.error
                any_failed = True
            else:
                extras = dict(seg.extras or {})
                compression = compress_read_text(
                    seg.text,
                    entry_id=eid,
                    args=dict(read_args),
                    extras=extras,
                    pipeline=pipeline.name,
                    kind=file_row.kind,
                    query=ctx.user_message,
                    source_name=str(read_args.get("member_path") or entry.display_name or ""),
                    source_ext=str(file_row.original_ext or ""),
                    settings=compression_settings,
                )
                entry_dict["text"] = compression.text
                if compression.compressed:
                    extras["read_compression"] = compression.metadata()
                if extras:
                    entry_dict["extras"] = extras
            if seg.extras and "extras" not in entry_dict:
                entry_dict["extras"] = seg.extras
            result_obj["reads"].append(entry_dict)
        if any_failed:
            result_obj["ok"] = False
        results.append(result_obj)

    overall_ok = all(r.get("ok") for r in results)
    return {
        "ok": overall_ok,
        "results": results,
        "count": len(results),
    }


def _expand_reads(reads_args: list[Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for raw in reads_args:
        read_args = dict(raw) if isinstance(raw, Mapping) else {}
        raw_patterns = read_args.get("patterns")
        patterns: list[str] = []
        if read_args.get("pattern"):
            patterns.append(str(read_args["pattern"]))
        if isinstance(raw_patterns, list):
            patterns.extend(str(item) for item in raw_patterns if str(item))

        if not raw_patterns:
            read_args.pop("patterns", None)
            expanded.append(read_args)
            continue

        seen: set[str] = set()
        for pattern in patterns:
            if pattern in seen:
                continue
            seen.add(pattern)
            item = dict(read_args)
            item.pop("patterns", None)
            item["pattern"] = pattern
            expanded.append(item)
    return expanded


def _locator_diagnostic(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return read locator shape without logging user/file content values."""
    diag: dict[str, Any] = {}
    numeric_keys = (
        "offset",
        "max_chars",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
        "paragraph_start",
        "paragraph_end",
        "slide_start",
        "slide_end",
        "image_index",
        "context_lines",
        "max_matches",
        "match_offset",
    )
    for key in numeric_keys:
        value = args.get(key)
        if isinstance(value, int):
            diag[key] = value
    for key in _LOCATOR_VALUE_KEYS:
        if args.get(key):
            diag[f"has_{key}"] = True
    patterns = args.get("patterns")
    if isinstance(patterns, list):
        diag["patterns_count"] = len(patterns)
    return diag


def _segment_diagnostic(extras: Mapping[str, Any]) -> dict[str, Any]:
    """Return segment metadata shape without copying content-bearing values."""
    diag: dict[str, Any] = {}
    scalar_keys = (
        "offset",
        "char_count",
        "total_chars",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
        "total_pages",
        "paragraph_start",
        "paragraph_end",
        "total_paragraphs",
        "slide_start",
        "slide_end",
        "image_index",
        "total_slides",
        "match_count",
        "total_matches",
        "match_offset",
        "next_match_offset",
        "next_offset",
    )
    for key in scalar_keys:
        if key not in extras:
            continue
        value = extras.get(key)
        if isinstance(value, (bool, int, float, str)) or value is None:
            diag[key] = value
    for key in ("truncated", "has_more", "read_truncated", "source_truncated"):
        value = extras.get(key)
        if isinstance(value, bool):
            diag[key] = value
    count_keys = {
        "available_sheets": "available_sheets_count",
        "available": "available_members_count",
    }
    for key, out_key in count_keys.items():
        value = extras.get(key)
        if isinstance(value, list):
            diag[out_key] = len(value)
    return diag


def _error_diagnostic(error: str) -> str:
    lowered = error.casefold()
    if "not found" in lowered:
        return "not found"
    if "invalid regex" in lowered:
        return "invalid regex"
    if "integer" in lowered or "must be" in lowered:
        return "invalid locator"
    if "unsupported" in lowered or "does not support" in lowered:
        return "unsupported"
    if "empty result" in lowered or "no matches" in lowered:
        return "empty"
    return "error"


async def _safe_read_segment(
    *,
    pipeline: Pipeline,
    file_row: File,
    args: dict[str, Any],
    storage,
) -> SegmentResult:
    try:
        return await pipeline.read_segment(
            file_row=file_row, args=args, storage=storage,
        )
    except Exception as exc:  # noqa: BLE001
        return SegmentResult(
            error=f"{pipeline.name} read_segment crashed: {exc!r}",
        )
