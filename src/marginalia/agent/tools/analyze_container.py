"""analyze_container — DESIGN.md §10.1.

Lets the agent look INSIDE a container entry (zip / tar / 7z / rar /
.gz / etc.) without ever materializing the inner files as standalone
entries.

Three things the caller can do in one invocation (extraction is shared):

  - list_files: optionally filter by glob pattern; returns paths + sizes
  - read_files: pass a list of {path, locations: [{unit, value}]} to read
                specific sections (units: lines, bytes, whole)
  - search:     substring or regex across all kept files; returns hits
                with file path, line number, surrounding context

A `container_path` reference (used in citations) is just the inner path:
  [^a]: entry_id=<container>, container_path=src/auth/login.py, lines=42-58
  → caller resolves via this tool

Safety: extraction limits and tempdir cleanup are owned by
`storage.open_archive`. This tool is just routing on top.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.pipelines.archive import _is_listable
from marginalia.repositories import entries as entries_repo
from marginalia.storage import get_storage, open_archive

log = logging.getLogger(__name__)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["container_entry_id"],
    "properties": {
        "container_entry_id": {
            "type": "string",
            "description": (
                "Entry UUID (or short hex prefix, ≥ 8 chars) of the "
                "container. NOT a file name — resolve via search_metadata "
                "or list_folder first."
            ),
        },
        "list_files": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "glob": {
                    "type": "string",
                    "description": "Glob pattern, e.g. 'src/**/*.py'.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "offset": {
                    "type": "integer", "minimum": 0,
                    "description": "Skip first N matching members. Default 0.",
                },
            },
        },
        "read_files": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {
                    "path": {"type": "string"},
                    "locations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["unit", "value"],
                            "properties": {
                                "unit": {"type": "string",
                                         "enum": ["lines", "bytes", "whole"]},
                                "value": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
        "search": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pattern": {"type": "string"},
                "regex": {"type": "boolean"},
                "max_hits": {"type": "integer", "minimum": 1, "maximum": 200},
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
                "offset": {
                    "type": "integer", "minimum": 0,
                    "description": "Skip first N hits. Default 0.",
                },
            },
        },
    },
}


@tool(
    name="analyze_container",
    description=(
        "Inspect inside a container entry (any archive shape: zip / tar / "
        "7z / rar / .gz / etc.). Combine list_files (glob), read_files "
        "(path + line ranges), and search (substring or regex). Inner "
        "files are NEVER materialised as standalone entries — references "
        "use container_path."
    ),
    schema=SCHEMA,
)
async def analyze_container(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    raw_id = args["container_entry_id"]
    container_id, err = await entries_repo.resolve_entry_id_prefix(db, raw_id)
    if err is not None:
        return {"error": err, "entry_id": raw_id}
    pair = await entries_repo.get_live_with_file(db, container_id)
    if pair is None:
        return {"error": "container entry not found", "entry_id": container_id}
    entry, file_row = pair
    if file_row.kind != "container":
        return {"error": f"entry kind is {file_row.kind!r}, not 'container'",
                "entry_id": container_id}

    storage = get_storage()
    body = bytearray()
    async for chunk in storage.get(file_row.storage_key):
        body.extend(chunk)

    filename = entry.display_name or "archive"
    with open_archive(bytes(body), filename) as session:
        # Filter out noise + git internals + path-traversal members so
        # the agent works against the same listing the ingest pipeline
        # produced.
        unsafe = session.unsafe_basenames
        visible = [
            m for m in session.members if _is_listable(m.path, unsafe)
        ]
        out: dict[str, Any] = {
            "display_name": entry.display_name,
            "file_count": len(visible),
        }
        if "list_files" in args:
            out["files"] = _do_list(visible, args["list_files"] or {})
        if "read_files" in args:
            out["reads"] = _do_reads(session, visible, args["read_files"] or [])
        if "search" in args:
            out["search"] = _do_search(session, visible, args["search"] or {})
        return out


# ---- list_files ------------------------------------------------------------

def _do_list(members, params: Mapping[str, Any]) -> dict[str, Any]:
    glob_pat = params.get("glob")
    limit = min(int(params.get("limit") or 200), 1000)
    offset = max(0, int(params.get("offset") or 0))
    matched_total = 0
    matches = []
    for m in members:
        if glob_pat and not _glob_match(m.path, glob_pat):
            continue
        matched_total += 1
        if matched_total <= offset:
            continue
        if len(matches) < limit:
            matches.append({"path": m.path, "size": m.size})
    has_more = matched_total > offset + len(matches)
    out = {
        "matches": matches,
        "count": len(matches),
        "total": matched_total,
        "glob": glob_pat,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(matches)
    return out


def _glob_match(path: str, pattern: str) -> bool:
    if "**" in pattern:
        regex_pat = fnmatch.translate(pattern).replace(r"(?s:.*)", ".*")
        return re.match(regex_pat, path) is not None
    return fnmatch.fnmatch(path, pattern)


# ---- read_files ------------------------------------------------------------

def _do_reads(
    session, visible, requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    visible_paths = {m.path for m in visible}
    out: list[dict[str, Any]] = []
    for req in requests:
        path = req.get("path")
        if not path or path not in visible_paths:
            out.append({"path": path, "error": "path not found in container"})
            continue
        try:
            body = session.read_bytes(path)
        except Exception as exc:
            out.append({"path": path, "error": f"read failed: {exc!r}"})
            continue
        text = _decode(body)
        locs = req.get("locations") or [{"unit": "whole", "value": ""}]
        location_results = [_extract_location(text, loc) for loc in locs]
        out.append({"path": path, "size": session.get(path).size,
                    "locations": location_results})
    return out


def _extract_location(text: str, loc: Mapping[str, Any]) -> dict[str, Any]:
    unit = loc.get("unit")
    value = loc.get("value") or ""
    if unit == "whole":
        return {"unit": "whole", "value": "", "text": text[:32_000]}
    if unit == "lines":
        try:
            start_s, end_s = value.split("-")
            start = max(1, int(start_s))
            end = int(end_s)
        except ValueError:
            return {"unit": unit, "value": value, "error": "value must be 'start-end'"}
        lines = text.splitlines()
        sliced = lines[start - 1:end]
        return {"unit": unit, "value": value,
                "text": "\n".join(sliced), "line_count": len(sliced)}
    if unit == "bytes":
        try:
            start, end = (int(x) for x in value.split("-"))
        except ValueError:
            return {"unit": unit, "value": value, "error": "value must be 'start-end'"}
        b = text.encode("utf-8", errors="replace")
        return {"unit": unit, "value": value,
                "text": b[start:end + 1].decode("utf-8", "replace")}
    return {"unit": unit, "value": value, "error": "unknown unit"}


# ---- search ----------------------------------------------------------------

def _do_search(
    session, visible, params: Mapping[str, Any],
) -> dict[str, Any]:
    pattern = (params.get("pattern") or "").strip()
    if not pattern:
        return {"error": "pattern is required"}
    regex_mode = bool(params.get("regex"))
    max_hits = min(int(params.get("max_hits") or 50), 200)
    ctx_lines = min(int(params.get("context_lines") or 1), 10)
    offset = max(0, int(params.get("offset") or 0))
    if regex_mode:
        try:
            pat = re.compile(pattern)
        except re.error as e:
            return {"error": f"invalid regex: {e!r}"}
    else:
        pat = re.compile(re.escape(pattern), re.IGNORECASE)

    hits: list[dict[str, Any]] = []
    match_index = 0
    has_more = False
    done = False
    for m in visible:
        if done:
            break
        try:
            body = session.read_bytes(m.path)
        except Exception:
            continue
        text = _decode(body)
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            match_index += 1
            if match_index <= offset:
                continue
            if len(hits) >= max_hits:
                has_more = True
                done = True
                break
            lo = max(0, i - ctx_lines)
            hi = min(len(lines), i + ctx_lines + 1)
            hits.append({
                "path": m.path,
                "line": i + 1,
                "match": line,
                "context": "\n".join(lines[lo:hi]),
            })
    out = {
        "hits": hits,
        "count": len(hits),
        "truncated": len(hits) >= max_hits,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(hits)
    return out


def _decode(b: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")
