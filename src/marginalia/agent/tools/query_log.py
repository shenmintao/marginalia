"""query_log — design.md §10.1.

Lightweight log filter — no SQL. Reads a log/text/jsonl file line-by-line
from storage and applies user-supplied filters:
  - `pattern`:    case-insensitive substring (or regex if `regex=True`)
  - `level`:      common log-level prefixes (DEBUG/INFO/WARN/ERROR/FATAL/CRITICAL)
  - `since` / `until`:  ISO-8601 timestamps if the log line begins with one
  - `limit`:      cap on returned matches (default 200, max 1000)

Returns the matching line + its 1-indexed line number.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import File, FileEntry
from marginalia.storage import get_storage


MAX_LINES_TO_SCAN = 200_000
DEFAULT_LIMIT = 200
MAX_LIMIT = 1_000


_LEVEL_RE = re.compile(
    r"\b(DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL|TRACE)\b",
    re.IGNORECASE,
)
# Loose ISO-8601 detector: 2024-03-12 14:30:45 / 2024-03-12T14:30:45Z / etc.
_TS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)",
)


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entry_id"],
    "properties": {
        "entry_id": {"type": "string"},
        "pattern": {"type": "string"},
        "regex": {"type": "boolean", "description": "Treat pattern as regex."},
        "level": {
            "type": "string",
            "enum": ["DEBUG", "INFO", "WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "TRACE"],
            "description": "Match log lines whose level matches.",
        },
        "since": {"type": "string", "description": "ISO-8601 lower bound."},
        "until": {"type": "string", "description": "ISO-8601 upper bound."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
    },
}


@tool(
    name="query_log",
    description=(
        "Filter a log / jsonl / plain-text entry line-by-line. Supports "
        "substring or regex pattern, common log levels, and ISO-8601 "
        "since/until time bounds. Returns matching lines with line numbers."
    ),
    schema=SCHEMA,
)
async def query_log(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    entry_id = args["entry_id"]
    pattern = args.get("pattern")
    is_regex = bool(args.get("regex"))
    level = (args.get("level") or "").upper() or None
    since_str = args.get("since")
    until_str = args.get("until")
    limit = min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)

    pat = _compile_pattern(pattern, is_regex)
    since_ts = _parse_iso(since_str)
    until_ts = _parse_iso(until_str)

    pair = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id == entry_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        return {"error": "entry not found", "entry_id": entry_id}
    entry, file_row = pair

    storage = get_storage()
    buf = bytearray()
    async for chunk in storage.get(file_row.storage_key):
        buf.extend(chunk)
    text = _decode(bytes(buf))

    hits: list[dict[str, Any]] = []
    line_no = 0
    for line in text.splitlines():
        line_no += 1
        if line_no > MAX_LINES_TO_SCAN:
            break
        if pat is not None and not pat.search(line):
            continue
        if level is not None:
            m = _LEVEL_RE.search(line)
            if m is None:
                continue
            seen = m.group(1).upper()
            if seen.startswith("WARN") and level.startswith("WARN"):
                pass  # WARN matches WARNING and vice-versa
            elif seen != level:
                continue
        if since_ts is not None or until_ts is not None:
            ts = _extract_ts(line)
            if ts is None:
                continue
            if since_ts is not None and ts < since_ts:
                continue
            if until_ts is not None and ts > until_ts:
                continue
        hits.append({"line": line_no, "text": line})
        if len(hits) >= limit:
            break

    return {
        "display_name": entry.display_name,
        "matches": hits,
        "match_count": len(hits),
        "scanned_lines": line_no,
        "truncated": len(hits) >= limit,
    }


def _compile_pattern(p: str | None, regex: bool) -> re.Pattern | None:
    if not p:
        return None
    if regex:
        try:
            return re.compile(p, re.IGNORECASE)
        except re.error:
            return None
    return re.compile(re.escape(p), re.IGNORECASE)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_ts(line: str) -> datetime | None:
    m = _TS_RE.search(line)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")
