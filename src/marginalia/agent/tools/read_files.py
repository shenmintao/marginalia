"""read_files — design.md §10.1.

Batch-read file content. Each request describes one entry_id + a list of
locations to extract. Locations may be:

  unit='section'  value=section_id   (looked up in files.description.sections)
  unit='lines'    value='10-50'      (1-indexed inclusive; only valid for text)
  unit='heading'  value='Pipeline'   (heading text in description.sections)
  unit='bytes'    value='0-65535'    (byte offsets; raw)
  unit='pages'    value='3-5'        (PDF only — V1 not implemented; rejected)

Optional `search` runs an in-memory ILIKE-style match across the file body
(only used for text/code/log files). Returns the matched windows with
surrounding context.

The handler opens each unique storage_key exactly once per call (so multiple
requests against the same file share one storage round-trip).
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import File, FileEntry
from marginalia.storage import get_storage
from marginalia.storage.base import StorageBackend

MAX_BYTES_PER_REQUEST = 256 * 1024
SEARCH_CONTEXT_CHARS = 200
MAX_HITS_PER_SEARCH = 20


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["requests"],
    "properties": {
        "requests": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id"],
                "properties": {
                    "entry_id": {"type": "string"},
                    "locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["unit", "value"],
                            "properties": {
                                "unit": {
                                    "type": "string",
                                    "enum": ["section", "lines", "heading", "bytes", "pages"],
                                },
                                "value": {"type": "string"},
                            },
                        },
                    },
                    "search": {
                        "type": "string",
                        "description": "ILIKE-style substring; returns windowed hits.",
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
        "extraction locations (sections/headings/lines/bytes) and/or a "
        "free-text search. Same-file requests are batched into one storage "
        "open. Use AFTER read_entries_metadata identified relevant sections."
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
        return {"results": [], "count": 0}

    # Resolve entry_id → (file_row, entry_row)
    entry_ids = [r["entry_id"] for r in requests if r.get("entry_id")]
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(FileEntry.id.in_(entry_ids))
        )
    ).all()
    by_entry: dict[str, tuple[FileEntry, File]] = {e.id: (e, f) for e, f in rows}

    storage = get_storage()

    # Cache file body (bytes) per file_id within this call
    body_cache: dict[str, bytes] = {}

    results: list[dict[str, Any]] = []
    for req in requests:
        eid = req["entry_id"]
        pair = by_entry.get(eid)
        if pair is None:
            results.append({"entry_id": eid, "error": "entry not found"})
            continue
        entry, file_row = pair
        if file_row.ingest_status != "done":
            results.append({"entry_id": eid, "error": "ingest not complete"})
            continue

        try:
            body = await _get_body(body_cache, storage, file_row)
        except Exception as exc:  # noqa: BLE001
            results.append({"entry_id": eid, "error": f"storage error: {exc!r}"})
            continue

        result_obj: dict[str, Any] = {
            "entry_id": eid,
            "display_name": entry.display_name,
            "kind": file_row.kind,
            "locations": [],
            "search_hits": None,
        }
        for loc in (req.get("locations") or []):
            block = _extract_location(loc, body, file_row)
            result_obj["locations"].append(block)

        search_q = (req.get("search") or "").strip()
        if search_q:
            result_obj["search_hits"] = _run_search(body, search_q)

        results.append(result_obj)

    return {"results": results, "count": len(results)}


async def _get_body(
    cache: dict[str, bytes], storage: StorageBackend, file_row: File
) -> bytes:
    if file_row.id in cache:
        return cache[file_row.id]
    buf = bytearray()
    async for chunk in storage.get(file_row.storage_key):
        buf.extend(chunk)
        if len(buf) > MAX_BYTES_PER_REQUEST:
            break
    cache[file_row.id] = bytes(buf[:MAX_BYTES_PER_REQUEST])
    return cache[file_row.id]


def _decode_text(body: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return body.decode(enc)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _extract_location(loc: dict, body: bytes, file_row: File) -> dict[str, Any]:
    unit = loc["unit"]
    value = loc["value"]

    if unit == "pages":
        return {"unit": unit, "value": value, "error": "pages not supported in V1"}

    if unit == "bytes":
        try:
            start, end = (int(x) for x in value.split("-"))
        except ValueError:
            return {"unit": unit, "value": value, "error": "value must be 'start-end'"}
        slice_bytes = body[max(0, start) : min(len(body), end + 1)]
        return {
            "unit": unit, "value": value,
            "text": _decode_text(slice_bytes),
        }

    text = _decode_text(body)

    if unit == "lines":
        try:
            start_s, end_s = value.split("-")
            start = max(1, int(start_s))
            end = int(end_s)
        except ValueError:
            return {"unit": unit, "value": value, "error": "value must be 'start-end'"}
        lines = text.splitlines()
        sliced = lines[start - 1 : end]
        return {
            "unit": unit, "value": value,
            "text": "\n".join(sliced),
            "line_count": len(sliced),
        }

    desc = file_row.description if isinstance(file_row.description, dict) else {}
    sections = desc.get("sections") if isinstance(desc, dict) else None
    if not isinstance(sections, list):
        return {"unit": unit, "value": value,
                "error": "no description.sections in file"}

    if unit == "section":
        for s in sections:
            if isinstance(s, dict) and s.get("id") == value:
                return _section_to_text(s, text, value, unit)
        return {"unit": unit, "value": value, "error": "section_id not found"}

    if unit == "heading":
        for s in sections:
            if isinstance(s, dict) and (s.get("title") or "").strip() == value.strip():
                return _section_to_text(s, text, value, unit)
        return {"unit": unit, "value": value, "error": "heading not found"}

    return {"unit": unit, "value": value, "error": f"unsupported unit: {unit}"}


def _section_to_text(s: dict, text: str, value: str, unit: str) -> dict[str, Any]:
    """Best-effort: dispatch on the section's anchor.unit if the upstream
    pipeline filled it. Otherwise fall back to title-marker scan."""
    anchor = s.get("anchor") or {}
    a_unit = anchor.get("unit")
    a_value = anchor.get("value")

    if a_unit == "lines" and isinstance(a_value, str) and "-" in a_value:
        try:
            start, end = (int(x) for x in a_value.split("-"))
            lines = text.splitlines()
            sliced = lines[max(0, start - 1) : end]
            return {
                "unit": unit, "value": value, "title": s.get("title"),
                "text": "\n".join(sliced),
            }
        except ValueError:
            pass

    title = (s.get("title") or "").strip()
    if title:
        idx = text.find(title)
        if idx != -1:
            return {
                "unit": unit, "value": value, "title": title,
                "text": text[idx : idx + 4096],
            }

    return {
        "unit": unit, "value": value, "title": s.get("title"),
        "summary": s.get("summary"),
        "key_terms": s.get("key_terms"),
        "note": "anchor not resolvable from body; returning section summary instead",
    }


def _run_search(body: bytes, query: str) -> list[dict[str, Any]]:
    text = _decode_text(body)
    lowered = text.lower()
    needle = query.lower()
    hits: list[dict[str, Any]] = []
    start = 0
    while len(hits) < MAX_HITS_PER_SEARCH:
        idx = lowered.find(needle, start)
        if idx == -1:
            break
        a = max(0, idx - SEARCH_CONTEXT_CHARS)
        b = min(len(text), idx + len(query) + SEARCH_CONTEXT_CHARS)
        hits.append({
            "match_offset": idx,
            "context": text[a:b],
        })
        start = idx + len(needle)
    return hits
