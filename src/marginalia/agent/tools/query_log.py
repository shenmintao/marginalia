from __future__ import annotations

import gzip
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.tools import ToolContext, tool
from marginalia.repositories import entries as entries_repo
from marginalia.storage import StorageBackend, get_storage

TOOL_NAME = "query_log"
DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
MAX_ENTRY_IDS = 50


_TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Inspect log files by filtering lines or aggregating patterns. Use this for exact "
        "log evidence: errors in a time window, repeated messages, top captured values, "
        "or day/hour distributions. Accepts one entry_id or many entry_ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "entry_id": {
                "type": "string",
                "description": "Log entry id or a unique entry id prefix.",
            },
            "entry_ids": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": MAX_ENTRY_IDS,
                "description": "Log entry ids or unique prefixes for cross-file log analysis.",
            },
            "operation": {
                "type": "string",
                "enum": ["filter_lines", "count_pattern", "top_values", "time_distribution"],
                "default": "filter_lines",
                "description": (
                    "filter_lines returns matching log lines; count_pattern counts matching "
                    "lines; top_values counts captured regex group values; time_distribution "
                    "counts matching lines by day or hour."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Substring or regex pattern. For top_values this should be a regex with "
                    "a named or positional capture group."
                ),
            },
            "regex": {
                "type": "boolean",
                "default": False,
                "description": "Treat pattern as a Python regular expression.",
            },
            "case_sensitive": {
                "type": "boolean",
                "default": False,
                "description": "Use case-sensitive substring or regex matching.",
            },
            "group_by": {
                "type": "string",
                "description": (
                    "For top_values: named capture group to count. For time_distribution: "
                    "day or hour; defaults to day."
                ),
            },
            "level": {
                "type": "string",
                "description": "Optional log level filter such as ERROR, WARN, INFO, or DEBUG.",
            },
            "since": {
                "type": "string",
                "description": "Inclusive ISO timestamp lower bound, for example 2024-03-12T10:00:00.",
            },
            "until": {
                "type": "string",
                "description": "Exclusive ISO timestamp upper bound, for example 2024-03-12T11:00:00.",
            },
            "line_start": {"type": "integer", "minimum": 1},
            "line_end": {"type": "integer", "minimum": 1},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_LIMIT,
                "default": DEFAULT_LIMIT,
                "description": "Maximum returned rows, matches, buckets, or values.",
            },
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Skip this many matched lines for filter_lines pagination.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

DESCRIPTION = str(_TOOL_SCHEMA["description"])
SCHEMA: dict[str, Any] = _TOOL_SCHEMA["input_schema"]  # type: ignore[assignment]

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
_SYSLOG_TS_RE = re.compile(r"([A-Z][a-z]{2}\s+\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})")


@dataclass(frozen=True)
class ScopedLine:
    number: int
    text: str


@tool(name=TOOL_NAME, description=DESCRIPTION, schema=SCHEMA)
async def query_log(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    del ctx
    return await handle(db, get_storage(), dict(args))


async def handle(db: Any, storage: StorageBackend, args: dict[str, Any]) -> dict[str, Any]:
    ids = _entry_ids(args)
    if not ids:
        return {"ok": False, "error": "entry_id or entry_ids is required"}
    if len(ids) > MAX_ENTRY_IDS:
        return {"ok": False, "error": f"entry_ids may contain at most {MAX_ENTRY_IDS} ids"}

    operation = str(args.get("operation") or "filter_lines")
    if operation not in {"filter_lines", "count_pattern", "top_values", "time_distribution"}:
        return {"ok": False, "error": f"unsupported operation: {operation}"}

    pattern = args.get("pattern")
    if operation in {"count_pattern", "top_values"} and not pattern:
        return {"ok": False, "error": f"{operation} requires pattern"}

    regex = bool(args.get("regex") or operation == "top_values")
    case_sensitive = bool(args.get("case_sensitive") or False)
    compiled, pattern_error = _compile_pattern(pattern, regex, case_sensitive)
    if pattern_error:
        return {"ok": False, "error": pattern_error}

    since = _parse_iso(args.get("since"))
    until = _parse_iso(args.get("until"))
    if args.get("since") and since is None:
        return {"ok": False, "error": "since must be an ISO timestamp"}
    if args.get("until") and until is None:
        return {"ok": False, "error": "until must be an ISO timestamp"}

    results = []
    for raw_id in ids:
        result = await _run_for_entry(db, storage, raw_id, args, operation, compiled, since, until)
        results.append(result)

    if len(results) == 1:
        return results[0]
    return {
        "ok": all(bool(item.get("ok")) for item in results),
        "operation": operation,
        "count": len(results),
        "results": results,
    }


def _entry_ids(args: dict[str, Any]) -> list[str]:
    values: list[str] = []
    entry_ids = args.get("entry_ids")
    if isinstance(entry_ids, list):
        values.extend(str(item).strip() for item in entry_ids if str(item).strip())
    entry_id = str(args.get("entry_id") or "").strip()
    if entry_id:
        values.insert(0, entry_id)

    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


async def _run_for_entry(
    db: Any,
    storage: StorageBackend,
    raw_id: str,
    args: dict[str, Any],
    operation: str,
    compiled: re.Pattern[str] | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    resolved, err = await entries_repo.resolve_entry_id_prefix(db, raw_id)
    if err:
        return {"ok": False, "entry_id": raw_id, "error": err}

    pair = await entries_repo.get_live_with_file(db, resolved)
    if not pair:
        return {"ok": False, "entry_id": resolved, "error": "entry not found"}
    entry, file_row = pair

    try:
        text = await _read_text(storage, file_row, entry.display_name)
    except Exception as exc:
        return {"ok": False, "entry_id": resolved, "error": f"failed to read log: {exc}"}

    limit = min(int(args.get("limit") or DEFAULT_LIMIT), MAX_LIMIT)
    offset = max(int(args.get("offset") or 0), 0)
    scoped = list(_scoped_lines(text, args, since, until))

    base = {
        "ok": True,
        "entry_id": resolved,
        "display_name": entry.display_name,
        "operation": operation,
        "scanned_lines": len(scoped),
    }

    if operation == "filter_lines":
        return base | _filter_lines(scoped, compiled, limit, offset)
    if operation == "count_pattern":
        return base | _count_pattern(scoped, compiled)
    if operation == "top_values":
        return base | _top_values(scoped, compiled, str(args.get("group_by") or ""), limit)
    if operation == "time_distribution":
        group_by = str(args.get("group_by") or "day").lower()
        return base | _time_distribution(scoped, compiled, group_by, limit)

    return {"ok": False, "entry_id": resolved, "error": f"unsupported operation: {operation}"}


async def _read_text(storage: StorageBackend, file_row: Any, display_name: str) -> str:
    buf = bytearray()
    async for chunk in storage.get(file_row.storage_key):
        buf.extend(chunk)
    data = bytes(buf)
    ext = str(getattr(file_row, "original_ext", "") or "").lower()
    name = str(display_name or "").lower()
    if data.startswith(b"\x1f\x8b") or ext == ".gz" or name.endswith(".gz"):
        data = gzip.decompress(data)
    return _decode(data)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _scoped_lines(
    text: str,
    args: dict[str, Any],
    since: datetime | None,
    until: datetime | None,
) -> list[ScopedLine]:
    level = str(args.get("level") or "").strip()
    line_start = int(args.get("line_start") or 1)
    raw_line_end = args.get("line_end")
    line_end = int(raw_line_end) if raw_line_end else None
    out: list[ScopedLine] = []

    for number, line in enumerate(text.splitlines(), start=1):
        if number < line_start:
            continue
        if line_end is not None and number > line_end:
            break
        if level and not _line_has_level(line, level):
            continue
        ts = _line_ts(line)
        if since is not None and (ts is None or ts < since):
            continue
        if until is not None and (ts is None or ts >= until):
            continue
        out.append(ScopedLine(number=number, text=line))
    return out


def _line_has_level(line: str, level: str) -> bool:
    needle = level.upper()
    tokens = re.split(r"[^A-Za-z]+", line.upper())
    if needle.startswith("WARN"):
        return any(token.startswith("WARN") for token in tokens)
    return needle in tokens


def _line_ts(line: str) -> datetime | None:
    match = _TS_RE.search(line)
    if not match:
        return None
    return _parse_iso(match.group(1))


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _compile_pattern(
    pattern: Any,
    regex: bool,
    case_sensitive: bool,
) -> tuple[re.Pattern[str] | None, str | None]:
    if not pattern:
        return None, None
    text = str(pattern)
    flags = 0 if case_sensitive else re.IGNORECASE
    source = text if regex else re.escape(text)
    try:
        return re.compile(source, flags), None
    except re.error as exc:
        return None, f"invalid regex pattern: {exc}"


def _filter_lines(
    lines: list[ScopedLine],
    compiled: re.Pattern[str] | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    total = 0
    for scoped in lines:
        if compiled is not None and not compiled.search(scoped.text):
            continue
        total += 1
        if total <= offset:
            continue
        if len(matches) < limit:
            matches.append({"line": scoped.number, "text": scoped.text})

    has_more = total > offset + len(matches)
    return {
        "matches": matches,
        "match_count": len(matches),
        "total_matches": total,
        "truncated": has_more,
        "has_more": has_more,
        "next_offset": offset + len(matches) if has_more else None,
    }


def _count_pattern(lines: list[ScopedLine], compiled: re.Pattern[str] | None) -> dict[str, Any]:
    total = 0
    for scoped in lines:
        if compiled is None or compiled.search(scoped.text):
            total += 1
    scanned = len(lines)
    return {
        "pattern": compiled.pattern if compiled is not None else None,
        "match_count": total,
        "line_count": scanned,
        "match_percent": round((total / scanned) * 100, 4) if scanned else 0.0,
    }


def _top_values(
    lines: list[ScopedLine],
    compiled: re.Pattern[str] | None,
    group_by: str,
    limit: int,
) -> dict[str, Any]:
    if compiled is None:
        return {"ok": False, "error": "top_values requires a regex pattern"}

    group_name = group_by.strip()
    counter: Counter[str] = Counter()
    matched_lines = 0
    missing_group = False

    for scoped in lines:
        for match in compiled.finditer(scoped.text):
            value = _capture_value(match, group_name)
            if value is None:
                missing_group = True
                continue
            matched_lines += 1
            counter[value] += 1

    values = [{"value": value, "count": count} for value, count in counter.most_common(limit)]
    result: dict[str, Any] = {
        "values": values,
        "unique_values": len(counter),
        "match_count": matched_lines,
        "truncated": len(counter) > limit,
    }
    if missing_group and not values:
        result["error"] = "pattern did not expose the requested capture group"
    return result


def _capture_value(match: re.Match[str], group_name: str) -> str | None:
    if group_name:
        try:
            return match.group(group_name)
        except (IndexError, KeyError):
            return None
    if match.lastgroup:
        return match.group(match.lastgroup)
    if match.lastindex:
        return match.group(1)
    return None


def _time_distribution(
    lines: list[ScopedLine],
    compiled: re.Pattern[str] | None,
    group_by: str,
    limit: int,
) -> dict[str, Any]:
    bucket_mode = "hour" if group_by == "hour" else "day"
    counter: Counter[str] = Counter()
    matched = 0
    with_timestamps = 0

    for scoped in lines:
        if compiled is not None and not compiled.search(scoped.text):
            continue
        matched += 1
        bucket = _time_bucket(scoped.text, bucket_mode)
        if bucket is None:
            continue
        with_timestamps += 1
        counter[bucket] += 1

    buckets = [{"bucket": bucket, "count": count} for bucket, count in sorted(counter.items())[:limit]]
    return {
        "group_by": bucket_mode,
        "buckets": buckets,
        "match_count": matched,
        "timestamped_count": with_timestamps,
        "truncated": len(counter) > limit,
    }


def _time_bucket(line: str, bucket_mode: str) -> str | None:
    ts = _line_ts(line)
    if ts is not None:
        return ts.strftime("%Y-%m-%d %H:00") if bucket_mode == "hour" else ts.strftime("%Y-%m-%d")

    syslog = _SYSLOG_TS_RE.search(line)
    if not syslog:
        return None
    day = " ".join(syslog.group(1).split())
    if bucket_mode == "hour":
        return f"{day} {syslog.group(2)}:00"
    return day
