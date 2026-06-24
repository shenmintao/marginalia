"""Vendored Headroom log/build-output compressor without CCR storage."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .adaptive_sizer import compute_optimal_k


class LogFormat(Enum):
    PYTEST = "pytest"
    NPM = "npm"
    CARGO = "cargo"
    MAKE = "make"
    JEST = "jest"
    GENERIC = "generic"


class LogLevel(Enum):
    ERROR = "error"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"
    DEBUG = "debug"
    TRACE = "trace"
    UNKNOWN = "unknown"


@dataclass(eq=False, slots=True)
class LogLine:
    line_number: int
    content: str
    level: LogLevel = LogLevel.UNKNOWN
    is_stack_trace: bool = False
    is_summary: bool = False
    score: float = 0.0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LogLine):
            return NotImplemented
        return self.line_number == other.line_number

    def __hash__(self) -> int:
        return hash(self.line_number)


@dataclass(slots=True)
class LogCompressorConfig:
    max_errors: int = 10
    error_context_lines: int = 3
    keep_first_error: bool = True
    keep_last_error: bool = True
    max_stack_traces: int = 3
    stack_trace_max_lines: int = 20
    max_warnings: int = 5
    dedupe_warnings: bool = True
    keep_summary_lines: bool = True
    max_total_lines: int = 100
    min_lines_to_compress: int = 50
    include_line_numbers: bool = True


@dataclass(slots=True)
class LogCompressionResult:
    compressed: str
    original: str
    original_line_count: int
    compressed_line_count: int
    format_detected: LogFormat
    compression_ratio: float
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def tokens_saved_estimate(self) -> int:
        chars_saved = len(self.original) - len(self.compressed)
        return max(0, chars_saved // 4)

    @property
    def lines_omitted(self) -> int:
        return self.original_line_count - self.compressed_line_count


class LogCompressor:
    """Compress build/test logs while preserving failures, traces, and summaries."""

    _FORMAT_PATTERNS = {
        LogFormat.PYTEST: [
            re.compile(r"^={3,} (FAILURES|ERRORS|test session|short test summary)"),
            re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED)\s+\["),
            re.compile(r"^collected \d+ items?"),
        ],
        LogFormat.NPM: [
            re.compile(r"^npm (ERR!|WARN|info|http)"),
            re.compile(r"^(>|added|removed) .+ packages?"),
        ],
        LogFormat.CARGO: [
            re.compile(r"^\s*(Compiling|Finished|Running|error\[E\d+\])"),
            re.compile(r"^warning: .+"),
        ],
        LogFormat.JEST: [
            re.compile(r"^(PASS|FAIL)\s+.+\.test\.(js|ts)"),
            re.compile(r"^Test Suites:"),
        ],
        LogFormat.MAKE: [
            re.compile(r"^make(\[\d+\])?: "),
            re.compile(r"^(gcc|g\+\+|clang).*-o "),
        ],
    }
    _LEVEL_PATTERNS = {
        LogLevel.ERROR: re.compile(r"\b(ERROR|error|Error|FATAL|fatal|Fatal|CRITICAL|critical)\b"),
        LogLevel.FAIL: re.compile(r"\b(FAIL|FAILED|fail|failed|Fail|Failed)\b"),
        LogLevel.WARN: re.compile(r"\b(WARN|WARNING|warn|warning|Warn|Warning)\b"),
        LogLevel.INFO: re.compile(r"\b(INFO|info|Info)\b"),
        LogLevel.DEBUG: re.compile(r"\b(DEBUG|debug|Debug)\b"),
        LogLevel.TRACE: re.compile(r"\b(TRACE|trace|Trace)\b"),
    }
    _STACK_TRACE_PATTERNS = [
        re.compile(r"^\s*Traceback \(most recent call last\)"),
        re.compile(r'^\s*File ".+", line \d+'),
        re.compile(r"^\s*at .+\(.+:\d+:\d+\)"),
        re.compile(r"^\s+at [\w.$]+\("),
        re.compile(r"^\s*--> .+:\d+:\d+"),
        re.compile(r"^\s*\d+:\s+0x[0-9a-f]+"),
    ]
    _SUMMARY_PATTERNS = [
        re.compile(r"^={3,}"),
        re.compile(r"^-{3,}"),
        re.compile(r"^\d+ (passed|failed|skipped|error|warning)"),
        re.compile(r"^(Tests?|Suites?):?\s+\d+"),
        re.compile(r"^(TOTAL|Total|Summary)"),
        re.compile(r"^(Build|Compile|Test).*(succeeded|failed|complete)"),
    ]

    def __init__(self, config: LogCompressorConfig | None = None) -> None:
        self.config = config or LogCompressorConfig()

    def compress(self, content: str, context: str = "", bias: float = 1.0) -> LogCompressionResult:
        del context
        lines = content.splitlines()
        if len(lines) < self.config.min_lines_to_compress:
            return LogCompressionResult(
                compressed=content,
                original=content,
                original_line_count=len(lines),
                compressed_line_count=len(lines),
                format_detected=LogFormat.GENERIC,
                compression_ratio=1.0,
            )
        log_format = self._detect_format(lines)
        log_lines = self._parse_lines(lines)
        selected = self._select_lines(log_lines, bias=bias)
        compressed, stats = self._format_output(selected, log_lines)
        ratio = len(compressed) / max(len(content), 1)
        return LogCompressionResult(
            compressed=compressed,
            original=content,
            original_line_count=len(lines),
            compressed_line_count=len(selected),
            format_detected=log_format,
            compression_ratio=ratio,
            stats=stats,
        )

    def _detect_format(self, lines: list[str]) -> LogFormat:
        sample = lines[:100]
        scores: dict[LogFormat, int] = {}
        for log_format, patterns in self._FORMAT_PATTERNS.items():
            score = 0
            for line in sample:
                if any(pattern.search(line) for pattern in patterns):
                    score += 1
            if score > 0:
                scores[log_format] = score
        if not scores:
            return LogFormat.GENERIC
        return max(scores, key=lambda key: scores[key])

    def _parse_lines(self, lines: list[str]) -> list[LogLine]:
        log_lines: list[LogLine] = []
        in_stack_trace = False
        stack_trace_lines = 0
        for idx, line in enumerate(lines):
            log_line = LogLine(line_number=idx, content=line)
            for level, pattern in self._LEVEL_PATTERNS.items():
                if pattern.search(line):
                    log_line.level = level
                    break
            if any(pattern.search(line) for pattern in self._STACK_TRACE_PATTERNS):
                in_stack_trace = True
                stack_trace_lines = 0
            if in_stack_trace:
                log_line.is_stack_trace = True
                stack_trace_lines += 1
                if stack_trace_lines > self.config.stack_trace_max_lines or not line.strip():
                    in_stack_trace = False
            if any(pattern.search(line) for pattern in self._SUMMARY_PATTERNS):
                log_line.is_summary = True
            log_line.score = self._score_line(log_line)
            log_lines.append(log_line)
        return log_lines

    @staticmethod
    def _score_line(log_line: LogLine) -> float:
        level_scores = {
            LogLevel.ERROR: 1.0,
            LogLevel.FAIL: 1.0,
            LogLevel.WARN: 0.5,
            LogLevel.INFO: 0.1,
            LogLevel.DEBUG: 0.05,
            LogLevel.TRACE: 0.02,
            LogLevel.UNKNOWN: 0.1,
        }
        score = level_scores.get(log_line.level, 0.1)
        if log_line.is_stack_trace:
            score += 0.3
        if log_line.is_summary:
            score += 0.4
        return min(1.0, score)

    def _select_lines(self, log_lines: list[LogLine], bias: float = 1.0) -> list[LogLine]:
        adaptive_max = compute_optimal_k(
            [line.content for line in log_lines],
            bias=bias,
            min_k=10,
            max_k=self.config.max_total_lines,
        )
        errors: list[LogLine] = []
        fails: list[LogLine] = []
        warnings: list[LogLine] = []
        summaries: list[LogLine] = []
        stack_traces: list[list[LogLine]] = []
        current_stack: list[LogLine] = []
        for line in log_lines:
            if line.level == LogLevel.ERROR:
                errors.append(line)
            elif line.level == LogLevel.FAIL:
                fails.append(line)
            elif line.level == LogLevel.WARN:
                warnings.append(line)
            if line.is_stack_trace:
                current_stack.append(line)
            elif current_stack:
                stack_traces.append(current_stack)
                current_stack = []
            if line.is_summary:
                summaries.append(line)
        if current_stack:
            stack_traces.append(current_stack)

        selected: list[LogLine] = []
        selected.extend(self._select_with_first_last(errors, self.config.max_errors))
        selected.extend(self._select_with_first_last(fails, self.config.max_errors))
        if warnings:
            selected.extend(
                (self._dedupe_similar(warnings) if self.config.dedupe_warnings else warnings)[
                    : self.config.max_warnings
                ]
            )
        for stack in stack_traces[: self.config.max_stack_traces]:
            selected.extend(stack[: self.config.stack_trace_max_lines])
        if self.config.keep_summary_lines:
            selected.extend(summaries)
        selected = self._add_context(log_lines, selected)
        selected = sorted(set(selected), key=lambda row: row.line_number)
        if len(selected) > adaptive_max:
            selected = sorted(selected, key=lambda row: row.score, reverse=True)[:adaptive_max]
            selected = sorted(selected, key=lambda row: row.line_number)
        return selected

    def _select_with_first_last(self, lines: list[LogLine], max_count: int) -> list[LogLine]:
        if len(lines) <= max_count:
            return lines
        selected: list[LogLine] = []
        if self.config.keep_first_error and lines:
            selected.append(lines[0])
        if self.config.keep_last_error and lines and lines[-1] not in selected:
            selected.append(lines[-1])
        remaining = max_count - len(selected)
        if remaining > 0:
            selected.extend(
                sorted(
                    (line for line in lines if line not in selected),
                    key=lambda row: row.score,
                    reverse=True,
                )[:remaining]
            )
        return selected

    @staticmethod
    def _dedupe_similar(lines: list[LogLine]) -> list[LogLine]:
        seen_patterns: set[str] = set()
        deduped: list[LogLine] = []
        for line in lines:
            normalized = re.sub(r"\d+", "N", line.content)
            normalized = re.sub(r"/[\w/]+/", "/PATH/", normalized)
            normalized = re.sub(r"0x[0-9a-f]+", "ADDR", normalized)
            if normalized not in seen_patterns:
                seen_patterns.add(normalized)
                deduped.append(line)
        return deduped

    def _add_context(self, all_lines: list[LogLine], selected: list[LogLine]) -> list[LogLine]:
        selected_indices = {line.line_number for line in selected}
        context_indices: set[int] = set()
        for idx in selected_indices:
            context_indices.update(range(max(0, idx - self.config.error_context_lines), idx))
            context_indices.update(
                range(idx + 1, min(len(all_lines), idx + self.config.error_context_lines + 1))
            )
        selected.extend(
            all_lines[idx]
            for idx in sorted(context_indices)
            if idx not in selected_indices and idx < len(all_lines)
        )
        return selected

    def _format_output(
        self,
        selected: list[LogLine],
        all_lines: list[LogLine],
    ) -> tuple[str, dict[str, int]]:
        stats = {
            "errors": sum(1 for line in all_lines if line.level == LogLevel.ERROR),
            "fails": sum(1 for line in all_lines if line.level == LogLevel.FAIL),
            "warnings": sum(1 for line in all_lines if line.level == LogLevel.WARN),
            "info": sum(1 for line in all_lines if line.level == LogLevel.INFO),
            "total": len(all_lines),
            "selected": len(selected),
        }
        if self.config.include_line_numbers:
            output_lines = [f"L{line.line_number + 1}: {line.content}" for line in selected]
        else:
            output_lines = [line.content for line in selected]
        omitted = len(all_lines) - len(selected)
        if omitted > 0:
            summary_parts = [
                f"{count} {level_name}"
                for level_name, count in (
                    ("ERROR", stats["errors"]),
                    ("FAIL", stats["fails"]),
                    ("WARN", stats["warnings"]),
                    ("INFO", stats["info"]),
                )
                if count > 0
            ]
            if summary_parts:
                output_lines.append(f"[{omitted} lines omitted: {', '.join(summary_parts)}]")
            else:
                output_lines.append(f"[{omitted} lines omitted]")
        return "\n".join(output_lines), stats
