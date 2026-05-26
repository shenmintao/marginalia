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
      { "paragraph_start": 4, "paragraph_end": 12 }, // DOCX only
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
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import File, FileEntry
from marginalia.pipelines.base import Pipeline, SegmentResult
from marginalia.pipelines.registry import resolve_pipeline
from marginalia.repositories import entries as entries_repo
from marginalia.storage import get_storage


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requests"],
    "properties": {
        "requests": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id"],
                "properties": {
                    "entry_id": {"type": "string"},
                    "reads": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                # generic chunking
                                "offset": {"type": "integer", "minimum": 0},
                                "max_chars": {"type": "integer", "minimum": 1},
                                # text-shaped
                                "line_start": {"type": "integer", "minimum": 1},
                                "line_end": {"type": "integer", "minimum": 1},
                                "section_id": {"type": "string"},
                                "heading": {"type": "string"},
                                # PDF
                                "page_start": {"type": "integer", "minimum": 1},
                                "page_end": {"type": "integer", "minimum": 1},
                                # DOCX
                                "paragraph_start": {"type": "integer", "minimum": 1},
                                "paragraph_end": {"type": "integer", "minimum": 1},
                                # pattern search
                                "pattern": {"type": "string"},
                                "context_lines": {"type": "integer", "minimum": 0},
                                "max_matches": {"type": "integer", "minimum": 1},
                                # container
                                "member_path": {"type": "string"},
                                # VLM-on-read: required when reading an
                                # image entry or a PDF flagged as
                                # OCR-only at ingest. The pipeline sends
                                # the original image / requested pages
                                # to the vision model with this question
                                # and returns the targeted answer.
                                "question": {"type": "string", "minLength": 1},
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
        "page_start/page_end for PDF; line_start/line_end / section_id / "
        "heading for text; pattern for regex search; member_path for "
        "container members). For image entries and PDFs that were "
        "OCR-indexed at ingest, pass `question` — the pipeline sends the "
        "image / rendered pages to the vision model and returns a "
        "targeted answer instead of frozen ingest-time text. Use AFTER "
        "read_entries_metadata identified relevant sections."
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

    entry_ids = [r["entry_id"] for r in requests if r.get("entry_id")]
    rows = await entries_repo.list_with_file_by_ids_any(db, entry_ids)
    by_entry: dict[str, tuple[FileEntry, File]] = {
        e.id: (e, f) for e, f in rows
    }

    storage = get_storage()
    results: list[dict[str, Any]] = []

    for req in requests:
        eid = req["entry_id"]
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
        for read_args in reads_args:
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
                entry_dict["text"] = seg.text
            if seg.extras:
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
