"""Unified model-view compression bridge backed by vendored Headroom transforms."""
from __future__ import annotations

import csv
import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from marginalia.config import get_settings
from marginalia.vendor.headroom.transforms.log_compressor import (
    LogCompressor,
    LogCompressorConfig,
)
from marginalia.vendor.headroom.transforms.search_compressor import (
    SearchCompressor,
    SearchCompressorConfig,
)
from marginalia.vendor.headroom.transforms.smart_crusher import (
    SmartCrusher,
    SmartCrusherConfig,
)
from marginalia.vendor.headroom.transforms.text_crusher import (
    TextCrusher,
    TextCrusherConfig,
)

log = logging.getLogger(__name__)

QUERY_TOOLS = {"query_log", "query_sql", "search_metadata"}

_SEARCH_LINE_RE = re.compile(r"(?m)^[^\s:]+:\d+:")
_CODE_LINE_RE = re.compile(
    r"^\s*(?:from\s+\S+\s+import\s+|import\s+|class\s+|def\s+|async\s+def\s+|"
    r"function\s+|export\s+|interface\s+|type\s+|struct\s+|enum\s+|impl\s+|package\s+)"
)
_LOG_SIGNAL_RE = re.compile(
    r"\b(error|exception|traceback|failed|failure|fatal|warn|warning|info|debug|trace)\b",
    re.IGNORECASE,
)
_JSON_EXTS = {".json", ".jsonl", ".ndjson"}
_TABLE_EXTS = {".csv", ".tsv", ".tab"}
_LOG_EXTS = {".log", ".out", ".err"}
_EXTRACTED_TEXT_EXTS = {".docx", ".pdf"}
_CODE_EXTS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql",
    ".lua", ".r", ".jl", ".ex", ".exs", ".erl", ".hrl",
}
_INGEST_TEXT_MIN_CHARS = 24_000
_ARCHIVE_PREVIEW_MIN_CHARS = 900


@dataclass(slots=True)
class CompressedText:
    text: str
    strategy: str
    original_chars: int
    compressed_chars: int
    extra: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "tokens_saved_estimate": max(0, self.original_chars - self.compressed_chars) // 4,
            **self.extra,
        }


def maybe_compress_tool_result_for_model(
    tool_name: str,
    payload: Any,
    *,
    context: str = "",
) -> dict[str, Any] | None:
    """Return a compact model-only tool payload, or ``None`` to keep original."""
    settings = get_settings()
    if not settings.compression_enabled or tool_name not in QUERY_TOOLS:
        return None

    original_text = _json_text(payload)
    if len(original_text) < settings.compression_min_chars:
        return None

    try:
        compressed = _compress_query_payload(tool_name, payload, context=context)
    except Exception as exc:  # noqa: BLE001 - compression must fail open
        log.debug("Query compression skipped for %s: %r", tool_name, exc)
        return None
    if compressed is None or not compressed.text.strip():
        return None

    envelope = _tool_envelope(tool_name, payload, compressed)
    envelope_text = _json_text(envelope)
    if not _beats_threshold(
        original_chars=len(original_text),
        compressed_chars=len(envelope_text),
        max_ratio=settings.compression_max_ratio,
    ):
        return None
    return envelope


def maybe_compress_ingest_view(
    body: str,
    *,
    kind: str,
    context: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Compress ingest prompt views for low-risk content classes."""
    settings = get_settings()
    if not settings.compression_enabled or len(body) < settings.compression_min_chars:
        return body, None

    try:
        compressed = _compress_ingest_text(body, kind=kind, context=context)
    except Exception as exc:  # noqa: BLE001 - compression must fail open
        log.debug("Ingest compression skipped for %s: %r", kind, exc)
        return body, None
    if compressed is None or not compressed.text.strip():
        return body, None
    if not _beats_threshold(
        original_chars=len(body),
        compressed_chars=len(compressed.text),
        max_ratio=settings.compression_max_ratio,
    ):
        return body, None
    return compressed.text, compressed.metadata()


def maybe_compress_read_view(
    body: str,
    *,
    pipeline: str = "",
    kind: str = "",
    context: str = "",
    target_ratio: float = 0.5,
    source_name: str = "",
    source_ext: str = "",
    member_path: str = "",
    allow_code: bool = False,
) -> CompressedText | None:
    """Compress a read_files model view using vendored Headroom transforms."""
    if not body.strip():
        return None
    try:
        return _compress_read_text(
            body,
            pipeline=pipeline,
            kind=kind,
            context=context,
            target_ratio=target_ratio,
            source_name=source_name,
            source_ext=source_ext,
            member_path=member_path,
            allow_code=allow_code,
        )
    except Exception as exc:  # noqa: BLE001 - compression must fail open
        log.debug("Read compression skipped for %s/%s: %r", pipeline, kind, exc)
        return None


def maybe_compress_ingest_aggregate_view(
    body: str,
    *,
    kind: str,
    context: str = "",
) -> tuple[str, dict[str, Any] | None]:
    """Compress long ingest aggregate prompts, never raw indexed chunks."""
    settings = get_settings()
    if not settings.compression_enabled or len(body) < settings.compression_min_chars:
        return body, None
    try:
        compressed = _compress_plain_text(
            body,
            context=context,
            target_ratio=_settings_target_ratio(settings, len(body)),
        )
    except Exception as exc:  # noqa: BLE001 - compression must fail open
        log.debug("Aggregate compression skipped for %s: %r", kind, exc)
        return body, None
    if compressed is None or not compressed.text.strip():
        return body, None
    if not _beats_threshold(
        original_chars=len(body),
        compressed_chars=len(compressed.text),
        max_ratio=settings.compression_max_ratio,
    ):
        return body, None
    meta = compressed.metadata()
    meta["aggregate"] = True
    meta["kind"] = kind
    return compressed.text, meta


def maybe_compress_archive_peeks(
    peeks: list[dict[str, Any]],
    *,
    context: str = "",
) -> list[dict[str, Any]]:
    """Compress archive member previews while keeping member_path reopen hints."""
    settings = get_settings()
    if not settings.compression_enabled or not peeks:
        return peeks
    min_chars = min(settings.compression_min_chars, _ARCHIVE_PREVIEW_MIN_CHARS)
    out: list[dict[str, Any]] = []
    for peek in peeks:
        item = dict(peek)
        preview = str(item.get("preview") or "")
        if len(preview) < min_chars:
            out.append(item)
            continue
        path = str(item.get("path") or "")
        kind = str(item.get("kind") or "")
        try:
            compressed = _compress_read_text(
                preview,
                pipeline=kind,
                kind=kind,
                context=context or path,
                target_ratio=_settings_target_ratio(settings, len(preview)),
                source_name=path,
                member_path=path,
                allow_code=True,
            )
        except Exception as exc:  # noqa: BLE001 - compression must fail open
            log.debug("Archive peek compression skipped for %s: %r", path, exc)
            out.append(item)
            continue
        if compressed is None or not compressed.text.strip():
            out.append(item)
            continue
        if not _beats_threshold(
            original_chars=len(preview),
            compressed_chars=len(compressed.text),
            max_ratio=settings.compression_max_ratio,
        ):
            out.append(item)
            continue
        item["preview"] = compressed.text
        meta = compressed.metadata()
        meta["reopen"] = {"member_path": path, "compress": False}
        item["compression"] = meta
        out.append(item)
    return out


def _compress_query_payload(
    tool_name: str,
    payload: Any,
    *,
    context: str,
) -> CompressedText | None:
    if tool_name == "query_log":
        search_text = _render_query_log_search(payload)
        if search_text:
            return _compress_search_text(search_text, context=context) or _compress_log_text(
                search_text,
                context=context,
            )

    records = _records_from_payload(payload)
    if records:
        return _compress_records(records, context=context, source_format=tool_name)
    if isinstance(payload, (dict, list)):
        return _compress_smart_json_content(
            _json_text(payload),
            context=context,
            original_chars=len(_json_text(payload)),
            source_format=tool_name,
            suffix="json",
        )
    return None


def _compress_ingest_text(body: str, *, kind: str, context: str) -> CompressedText | None:
    k = (kind or "").lower()
    if k == "log":
        return _compress_log_text(body, context=context)
    if k == "table":
        return _compress_table_text(body, context=context)

    ext = _route_ext(source_name=context, source_ext="", member_path="")
    route = _read_route(body, pipeline="", kind=k, source_name=context)
    if route == "json":
        return _compress_json_text(body, context=context)
    if route == "table":
        return _compress_table_text(body, context=context)
    if route == "log":
        return _compress_log_text(body, context=context)
    if route == "code":
        return None
    if (k in {"pdf", "docx"} or ext in _EXTRACTED_TEXT_EXTS) and len(body) >= _INGEST_TEXT_MIN_CHARS:
        return _compress_plain_text(body, context=context, target_ratio=0.6)
    return None


def _compress_read_text(
    body: str,
    *,
    pipeline: str,
    kind: str,
    context: str,
    target_ratio: float,
    source_name: str = "",
    source_ext: str = "",
    member_path: str = "",
    allow_code: bool = False,
) -> CompressedText | None:
    route = _read_route(
        body,
        pipeline=pipeline,
        kind=kind,
        source_name=source_name,
        source_ext=source_ext,
        member_path=member_path,
    )
    if route == "json":
        compressed = _compress_json_text(body, context=context)
    elif route == "table":
        compressed = _compress_table_text(body, context=context)
    elif route == "search":
        compressed = _compress_search_text(body, context=context)
    elif route == "log":
        compressed = _compress_log_text(body, context=context)
    elif route == "code":
        compressed = _compress_code_text(body, context=context, target_ratio=target_ratio) if allow_code else None
    else:
        compressed = _compress_plain_text(body, context=context, target_ratio=target_ratio)
    if compressed is not None:
        compressed.extra.setdefault("route", route)
    return compressed


def _compress_log_text(text: str, *, context: str) -> CompressedText | None:
    lines = text.splitlines()
    if len(lines) < 8:
        return None
    compressor = LogCompressor(
        LogCompressorConfig(
            max_total_lines=max(30, min(220, len(lines) // 4)),
            min_lines_to_compress=8,
            include_line_numbers=True,
        )
    )
    result = compressor.compress(text, context=context)
    if result.compressed == text or len(result.compressed) >= len(text):
        return None
    return CompressedText(
        text=result.compressed,
        strategy="headroom.log_compressor",
        original_chars=len(text),
        compressed_chars=len(result.compressed),
        extra={
            "line_count_before": result.original_line_count,
            "line_count_after": result.compressed_line_count,
            "lines_omitted": result.lines_omitted,
            "format": result.format_detected.value,
            "stats": result.stats,
            "lossy": True,
        },
    )


def _compress_search_text(text: str, *, context: str) -> CompressedText | None:
    compressor = SearchCompressor(
        SearchCompressorConfig(
            max_total_matches=30,
            max_matches_per_file=5,
            max_files=15,
        )
    )
    result = compressor.compress(text, context=context)
    if result.original_match_count < 4 or result.matches_omitted <= 0:
        return None
    if len(result.compressed) >= len(text):
        return None
    return CompressedText(
        text=result.compressed,
        strategy="headroom.search_compressor",
        original_chars=len(text),
        compressed_chars=len(result.compressed),
        extra={
            "match_count_before": result.original_match_count,
            "match_count_after": result.compressed_match_count,
            "matches_omitted": result.matches_omitted,
            "files_affected": result.files_affected,
            "summaries": result.summaries,
            "lossy": True,
        },
    )


def _compress_json_text(text: str, *, context: str) -> CompressedText | None:
    try:
        json.loads(text)
    except (TypeError, ValueError):
        records = _records_from_jsonl(text)
        if records:
            return _compress_records(
                records,
                context=context,
                original_chars=len(text),
                source_format="jsonl",
            )
        return None
    return _compress_smart_json_content(
        text,
        context=context,
        original_chars=len(text),
        source_format="json",
        suffix="json",
    )


def _compress_table_text(text: str, *, context: str) -> CompressedText | None:
    records = _records_from_table_text(text)
    if not records:
        return None
    return _compress_records(
        records,
        context=context,
        original_chars=len(text),
        source_format="table-text",
        lossy=True,
    )


def _compress_records(
    records: list[dict[str, Any]],
    *,
    context: str,
    original_chars: int | None = None,
    source_format: str = "records",
    lossy: bool = False,
) -> CompressedText | None:
    if len(records) < 2:
        return None
    content = json.dumps(records, ensure_ascii=False, default=str, separators=(",", ":"))
    if source_format.startswith("json"):
        suffix = "json"
    elif "table" in source_format:
        suffix = "table"
    else:
        suffix = "records"
    return _compress_smart_json_content(
        content,
        context=context,
        original_chars=original_chars or len(content),
        source_format=source_format,
        suffix=suffix,
        record_count=len(records),
        lossy=lossy,
    )


def _compress_smart_json_content(
    content: str,
    *,
    context: str,
    original_chars: int,
    source_format: str,
    suffix: str,
    record_count: int | None = None,
    lossy: bool = False,
) -> CompressedText | None:
    crusher = SmartCrusher(SmartCrusherConfig(max_items_after_crush=24))
    result = crusher.crush(content, query=context)
    compressed = result.compressed
    if not compressed.strip() or len(compressed) >= original_chars:
        return None
    item_count = result.original_item_count or record_count
    return CompressedText(
        text=compressed,
        strategy=f"headroom.smart_crusher.{suffix}",
        original_chars=original_chars,
        compressed_chars=len(compressed),
        extra={
            "smart_strategy": result.strategy,
            "source_format": source_format,
            "record_count": item_count,
            "compressed_item_count": result.compressed_item_count or None,
            "lossy": lossy or bool(result.compressed_item_count and result.compressed_item_count < item_count),
        },
    )


def _compress_plain_text(text: str, *, context: str, target_ratio: float) -> CompressedText | None:
    ratio = _clamp_ratio(target_ratio)
    compressor = TextCrusher(TextCrusherConfig(target_ratio=ratio))
    result = compressor.compress(text, context=context, target_ratio=ratio)
    if result.compressed == text or len(result.compressed) >= len(text):
        return None
    return CompressedText(
        text=result.compressed,
        strategy="headroom.text_crusher",
        original_chars=len(text),
        compressed_chars=len(result.compressed),
        extra={
            "compression_ratio": round(len(result.compressed) / max(1, len(text)), 4),
            "target_ratio": ratio,
            "kept_segments": result.kept_segments,
            "total_segments": result.total_segments,
            "lossy": True,
        },
    )


def _compress_code_text(text: str, *, context: str, target_ratio: float) -> CompressedText | None:
    del text, context, target_ratio
    return None


def _read_route(
    text: str,
    *,
    pipeline: str,
    kind: str,
    source_name: str = "",
    source_ext: str = "",
    member_path: str = "",
) -> str:
    p = (pipeline or "").lower()
    k = (kind or "").lower()
    ext = _route_ext(source_name=source_name, source_ext=source_ext, member_path=member_path)
    if p == "spreadsheet" or k == "table" or ext in _TABLE_EXTS:
        return "table"
    if ext in _JSON_EXTS or _looks_json(text) or _looks_jsonl(text):
        return "json"
    if _looks_like_search(text):
        return "search"
    if p == "log" or k == "log" or ext in _LOG_EXTS or _looks_like_log(text):
        return "log"
    if k == "code" or ext in _CODE_EXTS or _looks_like_code(text):
        return "code"
    return "text"


def _records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("entries"), list):
        return [dict(item) for item in payload["entries"] if isinstance(item, dict)]
    if isinstance(payload.get("rows"), list):
        columns = [str(c) for c in payload.get("columns") or []]
        records: list[dict[str, Any]] = []
        for row in payload["rows"]:
            if isinstance(row, dict):
                records.append(dict(row))
            elif isinstance(row, list) and columns:
                records.append({
                    columns[idx] if idx < len(columns) else f"col_{idx + 1}": value
                    for idx, value in enumerate(row)
                })
        return records
    if isinstance(payload.get("results"), list):
        return [dict(item) for item in payload["results"] if isinstance(item, dict)]
    return []


def _records_from_jsonl(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    for line in lines:
        try:
            parsed = json.loads(line)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, dict):
            return []
        records.append(dict(parsed))
    return records


def _records_from_table_text(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    sheet = ""
    block: list[str] = []

    def flush() -> None:
        nonlocal block
        if block:
            records.extend(_records_from_table_block(block, sheet=sheet, start_row=len(records) + 1))
            block = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith("# Sheet:"):
            flush()
            sheet = line.removeprefix("# Sheet:").strip()
            continue
        if line.startswith("[...") and "omitted" in line:
            continue
        block.append(line)
    flush()
    return records


def _records_from_table_block(lines: list[str], *, sheet: str, start_row: int) -> list[dict[str, Any]]:
    delimiter = _infer_delimiter(lines)
    if not delimiter:
        return []
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter, skipinitialspace=True)
    parsed_rows = [[_clean_table_cell(cell) for cell in row] for row in reader]
    parsed_rows = [row for row in parsed_rows if len(row) >= 2]
    if not parsed_rows:
        return []

    header: list[str] | None = None
    data_rows = parsed_rows
    if len(parsed_rows) >= 2 and _looks_like_header(parsed_rows[0], parsed_rows[1:]):
        header = [_field_name(cell, idx) for idx, cell in enumerate(parsed_rows[0], start=1)]
        data_rows = parsed_rows[1:]

    out: list[dict[str, Any]] = []
    for offset, row in enumerate(data_rows):
        record: dict[str, Any] = {"row": start_row + offset}
        if sheet:
            record["sheet"] = sheet
        if header:
            for idx, value in enumerate(row):
                key = header[idx] if idx < len(header) else f"col_{idx + 1}"
                record[key] = value
        else:
            record.update({f"col_{idx}": value for idx, value in enumerate(row, start=1)})
        out.append(record)
    return out


def _infer_delimiter(lines: list[str]) -> str:
    sample = "\n".join(lines[:20])
    if not sample:
        return ""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters="\t|,")
        return str(dialect.delimiter)
    except csv.Error:
        counts = {delimiter: sample.count(delimiter) for delimiter in ("\t", "|", ",")}
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        return delimiter if count else ""


def _looks_like_header(first_row: list[str], data_rows: list[list[str]]) -> bool:
    if len(set(first_row)) != len(first_row):
        return False
    if any(not cell for cell in first_row):
        return False
    if any(_looks_numeric(cell) for cell in first_row):
        return False
    data_sample = data_rows[:10]
    return any(any(_looks_numeric(cell) for cell in row) for row in data_sample)


def _field_name(cell: str, idx: int) -> str:
    cleaned = re.sub(r"\W+", "_", cell.strip().lower()).strip("_")
    return cleaned or f"col_{idx}"


def _clean_table_cell(value: str) -> str:
    return value.strip().replace(r"\|", "|").replace("\\n", " ")


def _looks_numeric(value: str) -> bool:
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _render_query_log_search(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    lines: list[str] = []
    results = payload.get("results")
    if isinstance(results, list):
        for result in results:
            if isinstance(result, dict):
                _append_log_matches(lines, result)
    else:
        _append_log_matches(lines, payload)
    return "\n".join(lines)


def _append_log_matches(lines: list[str], result: dict[str, Any]) -> None:
    matches = result.get("matches")
    if not isinstance(matches, list):
        return
    name = str(result.get("display_name") or result.get("entry_id") or "log")
    for idx, item in enumerate(matches, start=1):
        if not isinstance(item, dict):
            continue
        raw_line = item.get("line", idx)
        try:
            line_no = int(raw_line)
        except (TypeError, ValueError):
            line_no = idx
        text = str(item.get("text") or "")
        lines.append(f"{name}:{line_no}:{text}")


def _tool_envelope(tool_name: str, payload: Any, compressed: CompressedText) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "ok": payload.get("ok", True) if isinstance(payload, dict) else True,
        "compressed_for_model": True,
        "tool": tool_name,
        "compression": compressed.metadata(),
        "compressed_text": compressed.text,
    }
    if isinstance(payload, dict):
        for key in (
            "count",
            "total",
            "row_count",
            "match_count",
            "total_matches",
            "truncated",
            "has_more",
            "next_offset",
            "operation",
            "columns",
            "column_fixes",
            "rewritten_sql",
        ):
            if key in payload:
                envelope[key] = payload[key]
    return envelope


def _looks_json(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith(("[", "{")):
        return False
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return False
    return True


def _looks_like_search(text: str) -> bool:
    return len(_SEARCH_LINE_RE.findall(text[:50_000])) >= 3


def _looks_jsonl(text: str) -> bool:
    return bool(_records_from_jsonl("\n".join(text.splitlines()[:25])))


def _looks_like_log(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 20:
        return False
    levelish = sum(1 for line in lines[:300] if _LOG_SIGNAL_RE.search(line))
    timestamped = sum(
        1
        for line in lines[:300]
        if re.match(r"\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2}\s+\d{1,2}", line)
    )
    return levelish >= 3 or timestamped >= 8


def _looks_like_code(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    hits = sum(1 for line in lines[:200] if _CODE_LINE_RE.search(line))
    brace_lines = sum(
        1 for line in lines[:200] if "{" in line or "}" in line or line.rstrip().endswith(":")
    )
    return hits >= 3 or (hits >= 1 and brace_lines >= 8)


def _route_ext(*, source_name: str, source_ext: str, member_path: str) -> str:
    for candidate in (member_path, source_name, source_ext):
        ext = _suffix(candidate)
        if ext:
            return ext
    return ""


def _suffix(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith(".") and "/" not in raw and "\\" not in raw:
        return raw
    path = PureWindowsPath(raw) if "\\" in raw else PurePosixPath(raw)
    name = path.name
    for suffix in (".jsonl", ".ndjson", ".tar.gz", ".tar.bz2", ".tar.xz"):
        if name.endswith(suffix):
            return suffix
    return PurePosixPath(name).suffix.lower()


def _settings_target_ratio(settings: Any, original_len: int) -> float:
    if original_len <= 0:
        return 0.5
    try:
        target_chars = int(getattr(settings, "compression_target_chars", 0) or 0)
    except (TypeError, ValueError):
        target_chars = 0
    if target_chars <= 0:
        return 0.6
    return _clamp_ratio(target_chars / original_len)


def _clamp_ratio(value: float) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = 0.5
    return min(0.8, max(0.1, ratio))


def _beats_threshold(*, original_chars: int, compressed_chars: int, max_ratio: float) -> bool:
    if original_chars <= 0:
        return False
    return compressed_chars < int(original_chars * max_ratio)


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)
