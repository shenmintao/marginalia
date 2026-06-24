"""Vendored Headroom TextCrusher port.

Ported from ``crates/headroom-core/src/transforms/text_crusher``. The Rust
implementation split prose into sentence/line segments, scores by recency,
BM25 relevance, and salience, then suppresses near-duplicates with word
shingles. This Python port keeps the same deterministic, extractive behavior
without depending on ``headroom._core``.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(slots=True)
class TextCrusherConfig:
    target_ratio: float = 0.5
    w_recency: float = 1.0
    w_relevance: float = 2.0
    w_salience: float = 1.5
    min_segment_chars: int = 12
    near_dup_threshold: float = 0.85
    min_segments_for_crush: int = 6


@dataclass(slots=True)
class TextCrusherResult:
    compressed: str
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    kept_segments: int
    total_segments: int


_TOKEN_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|\b\d{4,}\b|[a-zA-Z0-9_]+"
)
_KEYWORDS = {
    "error", "exception", "failed", "failure", "fail", "warning",
    "traceback", "assert", "todo", "fixme",
}


class _BM25Scorer:
    """Minimal BM25 scorer ported from Headroom's Rust/Python relevance scorer."""

    def __init__(self, k1: float = 1.5, b: float = 0.75, max_score: float = 10.0) -> None:
        self.k1 = k1
        self.b = b
        self.max_score = max_score

    def tokenize(self, text: str) -> list[str]:
        return [m.group(0).lower() for m in _TOKEN_PATTERN.finditer(text or "")]

    def score_batch(self, items: list[str], context: str) -> list[float]:
        context_tokens = self.tokenize(context)
        if not context_tokens:
            return [0.0 for _ in items]
        query_freq = Counter(context_tokens)
        tokenized = [self.tokenize(item) for item in items]
        avg_len = sum(len(tokens) for tokens in tokenized) / max(1, len(tokenized))
        return [self._score_tokens(tokens, query_freq, avg_len) for tokens in tokenized]

    def _score_tokens(self, doc_tokens: list[str], query_freq: Counter[str], avg_len: float) -> float:
        if not doc_tokens or not query_freq:
            return 0.0
        doc_freq = Counter(doc_tokens)
        doc_len = len(doc_tokens)
        avgdl = avg_len if avg_len > 0 else max(1, doc_len)
        raw = 0.0
        matched: list[str] = []
        idf = math.log(2.0)
        for term in sorted(query_freq):
            f = doc_freq.get(term, 0)
            if f <= 0:
                continue
            matched.append(term)
            numerator = f * (self.k1 + 1.0)
            denominator = f + self.k1 * (1.0 - self.b + self.b * doc_len / avgdl)
            raw += idf * numerator / denominator * query_freq[term]
        normalized = min(1.0, raw / self.max_score)
        if any(len(term) >= 8 for term in matched):
            normalized = min(1.0, normalized + 0.3)
        return max(0.0, normalized)


class TextCrusher:
    def __init__(self, config: TextCrusherConfig | None = None) -> None:
        self.config = config or TextCrusherConfig()
        self.scorer = _BM25Scorer()

    @staticmethod
    def _passthrough(content: str, n_segments: int) -> TextCrusherResult:
        tokens = len(content.split())
        return TextCrusherResult(
            compressed=content,
            original_tokens=tokens,
            compressed_tokens=tokens,
            compression_ratio=1.0,
            kept_segments=n_segments,
            total_segments=n_segments,
        )

    def compress(
        self,
        content: str,
        context: str = "",
        target_ratio: float | None = None,
    ) -> TextCrusherResult:
        cfg = self.config
        ratio = min(1.0, max(0.05, cfg.target_ratio if target_ratio is None else target_ratio))
        segments = _split_segments(content)
        if len(segments) < cfg.min_segments_for_crush:
            return self._passthrough(content, len(segments))

        n = len(segments)
        total_chars = sum(len(s) for s in segments)
        target_chars = max(1, int(total_chars * ratio))
        relevance = self.scorer.score_batch(segments, context)
        seg_tokens = [_tokens(segment) for segment in segments]

        scores: list[float] = []
        for idx, segment in enumerate(segments):
            recency = (idx + 1.0) / n
            words = segment.split()
            salient = sum(1 for word in words if _is_salient(word))
            salience = salient / (len(words) + 1.0)
            score = cfg.w_recency * recency + cfg.w_relevance * relevance[idx] + cfg.w_salience * salience
            if len(segment) < cfg.min_segment_chars:
                score *= 0.25
            scores.append(score)

        order = sorted(range(n), key=lambda i: (-scores[i], i))
        kept = [False] * n
        seen: set[str] = set()
        kept_chars = 0
        kept_count = 0
        for idx in order:
            if kept_chars >= target_chars:
                break
            shingle_set = _shingles(seg_tokens[idx], 3)
            if shingle_set:
                covered = sum(1 for s in shingle_set if s in seen) / len(shingle_set)
                if covered >= cfg.near_dup_threshold:
                    continue
            kept[idx] = True
            kept_count += 1
            seen.update(shingle_set)
            kept_chars += len(segments[idx])

        if kept_count == 0:
            return self._passthrough(content, n)

        compressed = "\n".join(segments[i] for i in range(n) if kept[i])
        original_tokens = len(content.split())
        compressed_tokens = len(compressed.split())
        return TextCrusherResult(
            compressed=compressed,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=(compressed_tokens / original_tokens if original_tokens else 1.0),
            kept_segments=kept_count,
            total_segments=n,
        )


def _split_segments(text: str) -> list[str]:
    segments: list[str] = []
    for line in text.split("\n"):
        trimmed = line.strip()
        if not trimmed:
            continue
        cur: list[str] = []
        prev_term = False
        for char in trimmed:
            if prev_term and char.isspace():
                segment = "".join(cur).strip()
                if segment:
                    segments.append(segment)
                cur = []
                prev_term = False
                continue
            cur.append(char)
            prev_term = char in ".!?"
        segment = "".join(cur).strip()
        if segment:
            segments.append(segment)
    return segments


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in re.finditer(r"[A-Za-z0-9_]+", text)]


def _shingles(words: list[str], k: int) -> set[str]:
    out: set[str] = set()
    if not words:
        return out
    if len(words) < k:
        for size in range(1, len(words) + 1):
            for idx in range(0, len(words) - size + 1):
                out.add("\x01".join(words[idx : idx + size]))
        return out
    for idx in range(0, len(words) - k + 1):
        out.add("\x01".join(words[idx : idx + k]))
    return out


def _is_salient(word: str) -> bool:
    if any(ch.isdigit() for ch in word):
        return True
    lower = word.strip(".,:;()[]{}<>!?/\\\"'").lower()
    if lower in _KEYWORDS:
        return True
    alpha = [ch for ch in word if ch.isalpha()]
    if len(alpha) >= 2 and all(ch.isupper() for ch in alpha):
        return True
    if "." in word:
        left, _, right = word.partition(".")
        if left and right and (left[0].isalpha() or left[0] == "_") and (right[0].isalpha() or right[0] == "_"):
            return True
    return False
