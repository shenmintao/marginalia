"""Vendored Headroom search-result compressor without CCR storage."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .adaptive_sizer import compute_optimal_k
from .error_detection import PRIORITY_PATTERNS_SEARCH


@dataclass(slots=True)
class SearchMatch:
    file: str
    line_number: int
    content: str
    score: float = 0.0


@dataclass(slots=True)
class FileMatches:
    file: str
    matches: list[SearchMatch] = field(default_factory=list)

    @property
    def first(self) -> SearchMatch | None:
        return self.matches[0] if self.matches else None

    @property
    def last(self) -> SearchMatch | None:
        return self.matches[-1] if self.matches else None


@dataclass(slots=True)
class SearchCompressorConfig:
    max_matches_per_file: int = 5
    always_keep_first: bool = True
    always_keep_last: bool = True
    max_total_matches: int = 30
    max_files: int = 15
    context_keywords: list[str] = field(default_factory=list)
    boost_errors: bool = True


@dataclass(slots=True)
class SearchCompressionResult:
    compressed: str
    original: str
    original_match_count: int
    compressed_match_count: int
    files_affected: int
    compression_ratio: float
    summaries: dict[str, str] = field(default_factory=dict)

    @property
    def tokens_saved_estimate(self) -> int:
        chars_saved = len(self.original) - len(self.compressed)
        return max(0, chars_saved // 4)

    @property
    def matches_omitted(self) -> int:
        return self.original_match_count - self.compressed_match_count


class SearchCompressor:
    """Compress grep/ripgrep-style ``file:line:content`` output."""

    _GREP_PATTERN = re.compile(r"^([^:]+):(\d+):(.*)$")
    _RG_CONTEXT_PATTERN = re.compile(r"^([^:-]+)[:-](\d+)[:-](.*)$")
    _PRIORITY_PATTERNS = PRIORITY_PATTERNS_SEARCH

    def __init__(self, config: SearchCompressorConfig | None = None) -> None:
        self.config = config or SearchCompressorConfig()

    def compress(self, content: str, context: str = "", bias: float = 1.0) -> SearchCompressionResult:
        file_matches = self._parse_search_results(content)
        if not file_matches:
            return SearchCompressionResult(
                compressed=content,
                original=content,
                original_match_count=0,
                compressed_match_count=0,
                files_affected=0,
                compression_ratio=1.0,
            )

        original_count = sum(len(fm.matches) for fm in file_matches.values())
        self._score_matches(file_matches, context)
        selected = self._select_matches(file_matches, bias=bias)
        compressed, summaries = self._format_output(selected, file_matches)
        compressed_count = sum(len(fm.matches) for fm in selected.values())
        ratio = len(compressed) / max(len(content), 1)
        return SearchCompressionResult(
            compressed=compressed,
            original=content,
            original_match_count=original_count,
            compressed_match_count=compressed_count,
            files_affected=len(file_matches),
            compression_ratio=ratio,
            summaries=summaries,
        )

    def _parse_search_results(self, content: str) -> dict[str, FileMatches]:
        file_matches: dict[str, FileMatches] = {}
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = self._GREP_PATTERN.match(line) or self._RG_CONTEXT_PATTERN.match(line)
            if not match:
                continue
            file_path, line_num, match_content = match.groups()
            fm = file_matches.setdefault(file_path, FileMatches(file=file_path))
            fm.matches.append(
                SearchMatch(
                    file=file_path,
                    line_number=int(line_num),
                    content=match_content,
                )
            )
        return file_matches

    def _score_matches(self, file_matches: dict[str, FileMatches], context: str) -> None:
        context_words = {word for word in context.lower().split() if len(word) > 2}
        for fm in file_matches.values():
            for match in fm.matches:
                score = 0.0
                content_lower = match.content.lower()
                score += sum(0.3 for word in context_words if word in content_lower)
                if self.config.boost_errors:
                    for idx, pattern in enumerate(self._PRIORITY_PATTERNS):
                        if pattern.search(match.content):
                            score += 0.5 - (idx * 0.1)
                score += sum(
                    0.4
                    for keyword in self.config.context_keywords
                    if keyword.lower() in content_lower
                )
                match.score = min(1.0, score)

    def _select_matches(
        self,
        file_matches: dict[str, FileMatches],
        bias: float = 1.0,
    ) -> dict[str, FileMatches]:
        selected: dict[str, FileMatches] = {}
        sorted_files = sorted(
            file_matches.items(),
            key=lambda item: sum(match.score for match in item[1].matches),
            reverse=True,
        )[: self.config.max_files]
        all_match_strings = [
            f"{file_path}:{match.line_number}:{match.content}"
            for file_path, fm in sorted_files
            for match in fm.matches
        ]
        adaptive_total = compute_optimal_k(
            all_match_strings,
            bias=bias,
            min_k=5,
            max_k=self.config.max_total_matches,
        )

        total_selected = 0
        for file_path, fm in sorted_files:
            if total_selected >= adaptive_total:
                break
            remaining_slots = min(
                self.config.max_matches_per_file,
                adaptive_total - total_selected,
            )
            file_selected: list[SearchMatch] = []
            if self.config.always_keep_first and fm.first is not None:
                file_selected.append(fm.first)
                remaining_slots -= 1
            if (
                self.config.always_keep_last
                and fm.last is not None
                and fm.last != fm.first
                and remaining_slots > 0
            ):
                file_selected.append(fm.last)
                remaining_slots -= 1
            for match in sorted(fm.matches, key=lambda row: row.score, reverse=True):
                if remaining_slots <= 0:
                    break
                if match not in file_selected:
                    file_selected.append(match)
                    remaining_slots -= 1
            file_selected.sort(key=lambda row: row.line_number)
            selected[file_path] = FileMatches(file=file_path, matches=file_selected)
            total_selected += len(file_selected)
        return selected

    @staticmethod
    def _format_output(
        selected: dict[str, FileMatches],
        original: dict[str, FileMatches],
    ) -> tuple[str, dict[str, str]]:
        lines: list[str] = []
        summaries: dict[str, str] = {}
        for file_path, fm in sorted(selected.items()):
            for match in fm.matches:
                lines.append(f"{match.file}:{match.line_number}:{match.content}")
            original_fm = original.get(file_path)
            if original_fm and len(original_fm.matches) > len(fm.matches):
                omitted = len(original_fm.matches) - len(fm.matches)
                summary = f"[... and {omitted} more matches in {file_path}]"
                lines.append(summary)
                summaries[file_path] = summary
        return "\n".join(lines), summaries
