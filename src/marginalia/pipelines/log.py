"""Log file pipeline (.log via stdlib parsing).

Treats a log file as line-oriented text with optional timestamps and
severity levels. Ingest extracts statistics (line count, time range,
level distribution, top error patterns) and a sampled body for the
LLM indexer; read_segment supports line ranges, severity filtering,
regex pattern with context, and the generic offset/max_chars chunking.

Recognized formats (probed in order, first match wins per line):
  - syslog-style:  "Jan 23 10:15:42 host process: ..."
  - ISO 8601:      "2026-05-24T10:15:42.123Z LEVEL message"
  - Apache common: "127.0.0.1 - - [24/May/2026:10:15:42 +0000] ..."
  - Bracketed:     "[2026-05-24 10:15:42] [ERROR] message"
  - Anything else: line is kept as-is, level inferred from substrings
                   (ERROR/WARN/etc.)
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from typing import Any

from marginalia.pipelines._text_indexer import index_extracted_text
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    SegmentResult,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)

MAX_LOG_BYTES = 50 * 1024 * 1024
MAX_INGEST_LINES = 400  # head + tail + sampled middles
DEFAULT_MAX_CHARS = 8000

# Severity tokens, ordered most-severe-first so we report the highest seen.
LEVEL_PATTERNS = (
    ("FATAL", re.compile(r"\b(fatal|panic|crit(?:ical)?)\b", re.IGNORECASE)),
    ("ERROR", re.compile(r"\b(err(?:or)?|exception|fail(?:ure|ed)?)\b",
                         re.IGNORECASE)),
    ("WARN",  re.compile(r"\b(warn(?:ing)?)\b", re.IGNORECASE)),
    ("INFO",  re.compile(r"\b(info)\b", re.IGNORECASE)),
    ("DEBUG", re.compile(r"\b(debug|trace)\b", re.IGNORECASE)),
)

# Timestamp probes — looking for the *start* of the line.
_RX_ISO = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)
_RX_SYSLOG = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"
)
_RX_BRACKETED = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:[.,]\d+)?)\]"
)
_RX_APACHE = re.compile(
    r"\[(?P<ts>\d{2}/[A-Z][a-z]{2}/\d{4}:\d{2}:\d{2}:\d{2}\s[+-]\d{4})\]"
)


@register_pipeline(
    mimes=("application/x-log",),
    exts=(".log",),
    ext_patterns=(
        # logrotate counter:  app.log.1 / app.log.42
        re.compile(r"\.log\.\d+$", re.IGNORECASE),
        # date-suffixed:      app.log-20260524 / app.log-2026-05-24
        re.compile(r"\.log-\d{4}(?:-?\d{2}){2}$", re.IGNORECASE),
    ),
    ext_overrides_mime=True,  # text/plain would otherwise win
)
class LogPipeline(Pipeline):
    name = "log"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        lines, indexed_bytes, read_truncated = await self._read_lines_with_meta(
            storage, ctx.storage_key,
        )
        stats = _summarize(lines)
        sampled = _sample_for_indexer(lines, stats)
        coverage = _log_coverage(
            total_bytes=ctx.size_bytes,
            indexed_bytes=indexed_bytes,
            loaded_lines=len(lines),
            sampled_lines=_sampled_line_count(sampled),
            read_truncated=read_truncated,
            sample_truncated=any("sample truncated" in line for line in sampled),
        )
        body = (
            f"Log file extracted view:\n"
            f"  loaded lines: {stats['line_count']}\n"
            f"  time range:   {stats['first_ts'] or '?'} -> {stats['last_ts'] or '?'}\n"
            f"  levels:       {stats['level_counts']}\n"
            f"\n--- sampled lines ---\n"
            + "\n".join(sampled)
        )
        return await index_extracted_text(
            body, ctx, kind="log", coverage=coverage,
        )

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Resolve args against the parsed log lines.

        Field priority:
          1. pattern                        → regex search w/ context
          2. level=ERROR|WARN|...           → only lines at that level+
          3. line_start / line_end          → byte-equivalent line range
          4. (default)                      → offset..offset+max_chars chunk

        `level` is a custom field; valid values are the keys in
        LEVEL_PATTERNS (FATAL/ERROR/WARN/INFO/DEBUG). When set together
        with `line_start/line_end`, level filters within the range.
        """
        lines = await self._read_lines(storage, file_row.storage_key)
        return self._slice(lines, args)

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first variant — used by ArchivePipeline for member peeks."""
        text = _decode_log_bytes(body[:MAX_LOG_BYTES])
        return self._slice(text.splitlines(), args)

    def _slice(
        self,
        lines: list[str],
        args: dict[str, Any],
    ) -> SegmentResult:
        offset = max(0, int(args.get("offset") or 0))
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
        if max_chars <= 0:
            max_chars = DEFAULT_MAX_CHARS

        pattern = (args.get("pattern") or "").strip()
        if pattern:
            scope_lines = lines
            line_offset = 0
            ls_raw = args.get("line_start")
            le_raw = args.get("line_end")
            if ls_raw or le_raw:
                try:
                    ls = max(1, int(ls_raw)) if ls_raw else 1
                    le = int(le_raw) if le_raw else len(lines)
                except (TypeError, ValueError):
                    return SegmentResult(error="line_start/line_end must be integers")
                if le < ls:
                    return SegmentResult(error="line_end must be >= line_start")
                ls = min(ls, max(1, len(lines)))
                le = max(ls, min(le, len(lines)))
                scope_lines = lines[ls - 1: le]
                line_offset = ls - 1
            return _log_pattern_search(
                lines=scope_lines, pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                line_offset=line_offset,
                total_lines_full=len(lines),
            )

        level_filter = (args.get("level") or "").strip().upper()
        ls_arg = args.get("line_start")
        le_arg = args.get("line_end")
        ls = max(1, int(ls_arg)) if ls_arg else 1
        le = int(le_arg) if le_arg else len(lines)
        ls = min(ls, len(lines)) if lines else 0
        le = max(ls, min(le, len(lines))) if lines else 0

        sliced = lines[ls - 1: le] if lines else []
        if level_filter:
            allowed = _level_at_or_above(level_filter)
            if allowed is None:
                return SegmentResult(
                    error=f"unknown level: {level_filter!r} "
                          f"(use FATAL/ERROR/WARN/INFO/DEBUG)",
                )
            sliced_pairs = [
                (i + ls, ln) for i, ln in enumerate(sliced)
                if _detect_level(ln) in allowed
            ]
            text = "\n".join(f"L{n}: {ln}" for n, ln in sliced_pairs)
            return _clamp_log(
                text, offset, max_chars,
                extras={
                    "level_filter": level_filter,
                    "line_start": ls, "line_end": le,
                    "match_count": len(sliced_pairs),
                    "total_lines": len(lines),
                },
            )

        if ls_arg or le_arg:
            text = "\n".join(sliced)
            return _clamp_log(
                text, offset, max_chars,
                extras={
                    "line_start": ls, "line_end": le,
                    "line_count": len(sliced),
                    "total_lines": len(lines),
                },
            )

        # Default: chunk read of the whole body.
        body = "\n".join(lines)
        return _clamp_log(
            body, offset, max_chars,
            extras={"total_lines": len(lines)},
        )

    @staticmethod
    async def _read_lines(
        storage: StorageBackend, key: str,
    ) -> list[str]:
        lines, _indexed_bytes, _read_truncated = (
            await LogPipeline._read_lines_with_meta(storage, key)
        )
        return lines

    @staticmethod
    async def _read_lines_with_meta(
        storage: StorageBackend, key: str,
    ) -> tuple[list[str], int, bool]:
        buf = bytearray()
        truncated = False
        async for chunk in storage.get(key):
            buf.extend(chunk)
            if len(buf) > MAX_LOG_BYTES:
                buf = bytearray(buf[:MAX_LOG_BYTES])
                truncated = True
                break
        return _decode_log_bytes(bytes(buf)).splitlines(), len(buf), truncated


# ---- parsing & summary helpers --------------------------------------------

def _detect_timestamp(line: str) -> str | None:
    for rx in (_RX_ISO, _RX_BRACKETED, _RX_SYSLOG):
        m = rx.match(line)
        if m:
            return m.group("ts")
    m = _RX_APACHE.search(line)
    if m:
        return m.group("ts")
    return None


def _detect_level(line: str) -> str | None:
    """Return one of FATAL/ERROR/WARN/INFO/DEBUG, or None."""
    for level, rx in LEVEL_PATTERNS:
        if rx.search(line):
            return level
    return None


_LEVEL_ORDER = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")


def _level_at_or_above(level: str) -> set[str] | None:
    """Return the set of levels at-or-more-severe than `level`."""
    try:
        idx = _LEVEL_ORDER.index(level)
    except ValueError:
        return None
    return set(_LEVEL_ORDER[idx:])


def _summarize(lines: list[str]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    first_ts: str | None = None
    last_ts: str | None = None
    for ln in lines:
        lvl = _detect_level(ln)
        counts[lvl or "OTHER"] += 1
        ts = _detect_timestamp(ln)
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
    return {
        "line_count": len(lines),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "level_counts": dict(counts),
    }


def _sample_for_indexer(
    lines: list[str], stats: dict[str, Any],
) -> list[str]:
    """Build a representative sample: head + tail + first errors/warns."""
    out: list[str] = []
    head_n = min(50, len(lines))
    out.extend(f"L{i+1}: {ln}" for i, ln in enumerate(lines[:head_n]))

    err_warn = [
        (i + 1, ln) for i, ln in enumerate(lines)
        if _detect_level(ln) in ("FATAL", "ERROR", "WARN")
    ][:60]
    if err_warn:
        out.append(f"\n--- {len(err_warn)} ERROR/WARN/FATAL lines ---")
        out.extend(f"L{i}: {ln}" for i, ln in err_warn)

    if len(lines) > head_n + 50:
        out.append("\n--- last 50 lines ---")
        for i, ln in enumerate(lines[-50:], start=len(lines) - 49):
            out.append(f"L{i}: {ln}")

    if len(out) > MAX_INGEST_LINES:
        out = out[:MAX_INGEST_LINES] + ["[... sample truncated ...]"]
    return out


def _sampled_line_count(sampled: list[str]) -> int:
    return sum(1 for line in sampled if line.startswith("L"))


def _log_coverage(
    *,
    total_bytes: int,
    indexed_bytes: int,
    loaded_lines: int,
    sampled_lines: int,
    read_truncated: bool,
    sample_truncated: bool,
) -> dict[str, Any]:
    byte_partial = read_truncated or indexed_bytes < total_bytes
    sample_partial = sampled_lines < loaded_lines or sample_truncated
    partial_reasons: list[str] = []
    if byte_partial:
        partial_reasons.append("byte_cap")
    if sample_partial:
        partial_reasons.append("log_sample")
    indexed_partial = bool(partial_reasons)
    return {
        "unit": "lines",
        "source_mode": "log_sample",
        "total_units": loaded_lines,
        "indexed_units": sampled_lines,
        "total_lines": loaded_lines,
        "indexed_lines": sampled_lines,
        "total_bytes": total_bytes,
        "indexed_bytes": indexed_bytes,
        "indexed_partial": indexed_partial,
        "partial_reasons": partial_reasons,
        "max_index_bytes": MAX_LOG_BYTES,
        "max_sample_lines": MAX_INGEST_LINES,
        "sampled": True,
        "chunked": False,
        "chunk_count": 1,
        "text_truncated": byte_partial or sample_truncated,
    }


# ---- read_segment helpers --------------------------------------------------

def _clamp_log(
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
    if not chunk:
        return SegmentResult(text="", error="empty result", extras=extras)
    return SegmentResult(text=chunk, extras=extras)


def _log_pattern_search(
    *, lines: list[str], pattern: str,
    context_lines: int, max_matches: int,
    match_offset: int = 0, line_offset: int = 0,
    total_lines_full: int | None = None,
) -> SegmentResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return SegmentResult(error=f"invalid regex: {exc}")

    full_total = total_lines_full if total_lines_full is not None else len(lines)

    all_hits: list[dict[str, Any]] = []
    for i, ln in enumerate(lines, start=1):
        for m in rx.finditer(ln):
            s = max(0, i - 1 - context_lines)
            e = min(len(lines), i + context_lines)
            all_hits.append({
                "line": i + line_offset,
                "match": m.group(0)[:200],
                "level": _detect_level(ln),
                "context": "\n".join(lines[s:e]),
            })

    total = len(all_hits)
    hits = all_hits[match_offset: match_offset + max_matches]
    has_more = (match_offset + len(hits)) < total

    extras: dict[str, Any] = {
        "pattern": pattern,
        "match_count": len(hits),
        "total_matches": total,
        "match_offset": match_offset,
        "has_more": has_more,
        "hits": hits,
        "total_lines": full_total,
    }
    if has_more:
        extras["next_match_offset"] = match_offset + len(hits)

    if not hits:
        if match_offset and total:
            err = f"match_offset {match_offset} exceeds total_matches {total}"
        else:
            err = "no matches"
        return SegmentResult(text="", error=err, extras=extras)

    rendered = "\n\n".join(
        f"[L{h['line']}{(' ' + h['level']) if h['level'] else ''}] "
        f"{h['match']}\n  ┊ {h['context']}"
        for h in hits
    )
    return SegmentResult(text=rendered, extras=extras)


def _decode_log_bytes(buf: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return buf.decode(enc)
        except UnicodeDecodeError:
            continue
    return buf.decode("utf-8", errors="replace")
