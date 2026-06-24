"""Vendored Headroom SmartCrusher core.

This is a dependency-free migration of Headroom's SmartCrusher transform core.
It keeps the statistical JSON compression behavior while removing SDK-only
CCR storage, telemetry, TOIN feedback, tokenizer, and ML relevance backends.

Arrays are compressed extractively: kept values come from the original data.
Object-key compression is only applied to large objects after nested arrays have
been processed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .adaptive_sizer import compute_optimal_k
from .error_detection import ERROR_KEYWORDS
from .text_crusher import _BM25Scorer

_UUID_PATTERN = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_NUMERIC_ID_PATTERN = re.compile(r"\b\d{4,}\b")
_HOSTNAME_PATTERN = re.compile(
    r"\b[a-zA-Z0-9][-a-zA-Z0-9]*\.[a-zA-Z0-9][-a-zA-Z0-9]*(?:\.[a-zA-Z]{2,})?\b"
)
_QUOTED_STRING_PATTERN = re.compile(r"['\"]([^'\"]{1,80})['\"]")
_EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ISO_DATETIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
_ISO_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CompressionStrategy(Enum):
    NONE = "none"
    SKIP = "skip"
    TIME_SERIES = "time_series"
    CLUSTER_SAMPLE = "cluster"
    TOP_N = "top_n"
    SMART_SAMPLE = "smart_sample"


class ArrayType(Enum):
    DICT_ARRAY = "dict_array"
    STRING_ARRAY = "string_array"
    NUMBER_ARRAY = "number_array"
    BOOL_ARRAY = "bool_array"
    NESTED_ARRAY = "nested_array"
    MIXED_ARRAY = "mixed_array"
    EMPTY = "empty"


@dataclass(slots=True)
class CrushResult:
    compressed: str
    original: str
    was_modified: bool
    strategy: str = "passthrough"
    original_item_count: int = 0
    compressed_item_count: int = 0


@dataclass(slots=True)
class SmartCrusherConfig:
    enabled: bool = True
    min_items_to_analyze: int = 5
    min_tokens_to_crush: int = 200
    variance_threshold: float = 2.0
    max_items_after_crush: int = 24
    preserve_change_points: bool = True
    dedup_identical_items: bool = True
    first_fraction: float = 0.3
    last_fraction: float = 0.15
    relevance_threshold: float = 0.22
    max_process_depth: int = 50


@dataclass(slots=True)
class FieldStats:
    name: str
    field_type: str
    count: int
    unique_count: int
    unique_ratio: float
    is_constant: bool
    constant_value: Any = None
    min_val: float | None = None
    max_val: float | None = None
    mean_val: float | None = None
    variance: float | None = None
    change_points: list[int] = field(default_factory=list)
    avg_length: float | None = None
    top_values: list[tuple[str, int]] = field(default_factory=list)


@dataclass(slots=True)
class ArrayAnalysis:
    item_count: int
    field_stats: dict[str, FieldStats]
    detected_pattern: str
    recommended_strategy: CompressionStrategy
    constant_fields: dict[str, Any]
    estimated_reduction: float


class SmartCrusher:
    """Statistical JSON compressor migrated from Headroom SmartCrusher."""

    def __init__(self, config: SmartCrusherConfig | None = None) -> None:
        self.config = config or SmartCrusherConfig()
        self._scorer = _BM25Scorer()

    def crush(self, content: str, query: str = "", bias: float = 1.0) -> CrushResult:
        if not self.config.enabled:
            return CrushResult(content, content, False)
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return CrushResult(content, content, False)

        processed, info, changed, counts = self._process_value(parsed, query_context=query, bias=bias)
        compressed = json.dumps(processed, ensure_ascii=False, separators=(",", ":"), default=str)
        original = content.strip()
        was_modified = changed or compressed != original
        return CrushResult(
            compressed=compressed if was_modified else content,
            original=content,
            was_modified=was_modified,
            strategy=info or ("json:minified" if was_modified else "passthrough"),
            original_item_count=counts[0],
            compressed_item_count=counts[1],
        )

    def _process_value(
        self,
        value: Any,
        *,
        depth: int = 0,
        query_context: str = "",
        bias: float = 1.0,
    ) -> tuple[Any, str, bool, tuple[int, int]]:
        if depth >= self.config.max_process_depth:
            return value, "", False, (0, 0)

        if isinstance(value, list):
            if len(value) >= self.config.min_items_to_analyze:
                arr_type = _classify_array(value)
                if arr_type == ArrayType.DICT_ARRAY:
                    crushed, strategy = self._crush_dict_array(value, query_context, bias=bias)
                    return crushed, strategy, len(crushed) != len(value), (len(value), len(crushed))
                if arr_type == ArrayType.STRING_ARRAY:
                    crushed, strategy = self._crush_string_array(value, bias=bias)
                    return crushed, strategy, len(crushed) != len(value), (len(value), len(crushed))
                if arr_type == ArrayType.NUMBER_ARRAY:
                    crushed, strategy = self._crush_number_array(value, bias=bias)
                    return crushed, strategy, len(crushed) != len(value), (len(value), len(crushed))
                if arr_type == ArrayType.MIXED_ARRAY:
                    crushed, strategy = self._crush_mixed_array(value, query_context, bias=bias)
                    return crushed, strategy, len(crushed) != len(value), (len(value), len(crushed))

            changed = False
            info_parts: list[str] = []
            original_total = 0
            compressed_total = 0
            processed_list = []
            for item in value:
                p_item, p_info, p_changed, p_counts = self._process_value(
                    item, depth=depth + 1, query_context=query_context, bias=bias
                )
                processed_list.append(p_item)
                changed = changed or p_changed
                if p_info:
                    info_parts.append(p_info)
                original_total += p_counts[0]
                compressed_total += p_counts[1]
            return processed_list, ",".join(info_parts), changed, (original_total, compressed_total)

        if isinstance(value, dict):
            changed = False
            info_parts: list[str] = []
            original_total = 0
            compressed_total = 0
            processed_dict: dict[str, Any] = {}
            for key, val in value.items():
                p_val, p_info, p_changed, p_counts = self._process_value(
                    val, depth=depth + 1, query_context=query_context, bias=bias
                )
                processed_dict[key] = p_val
                changed = changed or p_changed
                if p_info:
                    info_parts.append(p_info)
                original_total += p_counts[0]
                compressed_total += p_counts[1]

            if len(processed_dict) >= self.config.min_items_to_analyze:
                crushed_obj, strategy = self._crush_object(processed_dict, bias=bias)
                if len(crushed_obj) != len(processed_dict):
                    return crushed_obj, ",".join([*info_parts, strategy]), True, (
                        max(original_total, len(processed_dict)),
                        max(compressed_total, len(crushed_obj)),
                    )
            return processed_dict, ",".join(info_parts), changed, (original_total, compressed_total)

        return value, "", False, (0, 0)

    def _crush_dict_array(
        self,
        items: list[dict[str, Any]],
        query_context: str = "",
        *,
        bias: float = 1.0,
    ) -> tuple[list[dict[str, Any]], str]:
        n = len(items)
        item_strings = [_safe_json(item, sort_keys=True) for item in items]
        k_total, k_first, k_last, _ = self._compute_k_split(items, bias, item_strings=item_strings)
        if n <= k_total:
            deduped = self._deduplicate_items(items) if self.config.dedup_identical_items else items
            if len(deduped) != len(items):
                return deduped, f"dict:dedup({n}->{len(deduped)})"
            return items, "dict:passthrough"

        analysis = self._analyze_dict_array(items)
        if analysis.recommended_strategy == CompressionStrategy.SKIP:
            return items, "dict:skip:no_signal"

        keep_indices: set[int] = set(range(min(k_first, n)))
        if k_last:
            keep_indices.update(range(max(0, n - k_last), n))

        keep_indices.update(_detect_error_items_for_preservation(items, item_strings))
        keep_indices.update(_detect_structural_outliers(items))
        keep_indices.update(self._numeric_anomaly_indices(items, analysis))
        if self.config.preserve_change_points:
            keep_indices.update(self._change_point_windows(analysis, n))

        score_field = self._score_field(analysis, items)
        if score_field:
            top_count = max(1, min(k_total, self.config.max_items_after_crush) // 2)
            ranked = sorted(
                ((idx, item.get(score_field, 0)) for idx, item in enumerate(items)),
                key=lambda pair: _finite_float(pair[1]),
                reverse=True,
            )
            keep_indices.update(idx for idx, _ in ranked[:top_count])

        if query_context:
            anchors = extract_query_anchors(query_context)
            keep_indices.update(
                idx for idx, item in enumerate(items) if item_matches_anchors(item, anchors)
            )
            scores = self._scorer.score_batch(item_strings, query_context)
            relevance_budget = max(2, k_total // 4)
            relevant = sorted(
                ((score, idx) for idx, score in enumerate(scores) if score >= self.config.relevance_threshold),
                reverse=True,
            )
            keep_indices.update(idx for _, idx in relevant[:relevance_budget])

        keep_indices = self._deduplicate_indices_by_content(keep_indices, items)
        keep_indices = self._fill_remaining_slots(keep_indices, items, k_total)
        keep_indices = self._prioritize_indices(keep_indices, items, analysis, k_total)

        result = [items[idx].copy() for idx in sorted(keep_indices) if 0 <= idx < n]
        if len(result) >= n:
            return items, "dict:passthrough"
        strategy = f"dict:{analysis.recommended_strategy.value}({n}->{len(result)})"
        if score_field:
            strategy += f",score={score_field}"
        return result, strategy

    def _compute_k_split(
        self,
        items: list[Any],
        bias: float = 1.0,
        item_strings: list[str] | None = None,
    ) -> tuple[int, int, int, int]:
        if item_strings is None:
            item_strings = [_safe_json(item) for item in items]
        k_total = compute_optimal_k(
            item_strings,
            bias=bias,
            min_k=3,
            max_k=self.config.max_items_after_crush or None,
        )
        k_first = max(1, round(k_total * self.config.first_fraction))
        k_first = min(k_first, k_total)
        k_last = max(1, round(k_total * self.config.last_fraction))
        k_last = min(k_last, max(0, k_total - k_first))
        k_importance = max(0, k_total - k_first - k_last)
        return k_total, k_first, k_last, k_importance

    def _crush_string_array(self, items: list[str], bias: float = 1.0) -> tuple[list[str], str]:
        n = len(items)
        if n <= 8:
            return items, "string:passthrough"
        k_total, k_first, k_last, _ = self._compute_k_split(items, bias)

        error_indices = {
            idx
            for idx, value in enumerate(items)
            if any(keyword in value.lower() for keyword in ERROR_KEYWORDS)
        }
        lengths = [len(value) for value in items]
        anomaly_indices: set[int] = set()
        if len(lengths) > 1:
            mean_len = statistics.mean(lengths)
            std_len = statistics.stdev(lengths)
            if std_len > 0:
                anomaly_indices = {
                    idx
                    for idx, length in enumerate(lengths)
                    if abs(length - mean_len) > self.config.variance_threshold * std_len
                }

        keep_indices = set(range(min(k_first, n)))
        if k_last:
            keep_indices.update(range(max(0, n - k_last), n))
        keep_indices.update(error_indices)
        keep_indices.update(anomaly_indices)

        seen = {items[idx] for idx in keep_indices if 0 <= idx < n}
        remaining_budget = max(0, k_total - len(keep_indices))
        if remaining_budget > 0:
            stride = max(1, (n - 1) // (remaining_budget + 1))
            for idx in range(0, n, stride):
                if len(keep_indices) >= k_total + len(error_indices) + len(anomaly_indices):
                    break
                if idx not in keep_indices and items[idx] not in seen:
                    keep_indices.add(idx)
                    seen.add(items[idx])

        result = [items[idx] for idx in sorted(keep_indices)]
        if len(result) >= n:
            return items, "string:passthrough"
        strategy = f"string:adaptive({n}->{len(result)}"
        if error_indices:
            strategy += f",errors={len(error_indices)}"
        if anomaly_indices:
            strategy += f",anomalies={len(anomaly_indices)}"
        return result, strategy + ")"

    def _crush_number_array(
        self,
        items: list[int | float],
        bias: float = 1.0,
    ) -> tuple[list[int | float], str]:
        n = len(items)
        if n <= 8:
            return items, "number:passthrough"
        finite = [float(x) for x in items if _is_finite_number(x)]
        if not finite:
            return items, "number:no_finite"

        k_total, k_first, k_last, _ = self._compute_k_split(items, bias)
        mean_val = statistics.mean(finite)
        median_val = statistics.median(finite)
        std_val = statistics.stdev(finite) if len(finite) > 1 else 0.0
        sorted_finite = sorted(finite)
        p25 = _percentile_linear(sorted_finite, 0.25)
        p75 = _percentile_linear(sorted_finite, 0.75)

        outlier_indices: set[int] = set()
        if std_val > 0:
            for idx, value in enumerate(items):
                if _is_finite_number(value) and abs(float(value) - mean_val) > self.config.variance_threshold * std_val:
                    outlier_indices.add(idx)

        change_indices: set[int] = set()
        if self.config.preserve_change_points and n > 10 and std_val > 0:
            window = 5
            for idx in range(window, n - window):
                left = [float(items[j]) for j in range(idx - window, idx) if _is_finite_number(items[j])]
                right = [float(items[j]) for j in range(idx, idx + window) if _is_finite_number(items[j])]
                if left and right and abs(statistics.mean(right) - statistics.mean(left)) > self.config.variance_threshold * std_val:
                    change_indices.add(idx)

        keep_indices = set(range(min(k_first, n)))
        if k_last:
            keep_indices.update(range(max(0, n - k_last), n))
        keep_indices.update(outlier_indices)
        keep_indices.update(change_indices)

        remaining_budget = max(0, k_total - len(keep_indices))
        if remaining_budget > 0:
            stride = max(1, (n - 1) // (remaining_budget + 1))
            for idx in range(0, n, stride):
                if len(keep_indices) >= k_total + len(outlier_indices):
                    break
                keep_indices.add(idx)

        result = [items[idx] for idx in sorted(keep_indices)]
        if len(result) >= n:
            return items, "number:passthrough"
        strategy = (
            f"number:adaptive({n}->{len(result)},min={min(finite):.4g},max={max(finite):.4g},"
            f"mean={mean_val:.4g},median={median_val:.4g},stddev={std_val:.4g},"
            f"p25={p25:.4g},p75={p75:.4g})"
        )
        return result, strategy

    def _crush_mixed_array(
        self,
        items: list[Any],
        query_context: str = "",
        *,
        bias: float = 1.0,
    ) -> tuple[list[Any], str]:
        n = len(items)
        if n <= 8:
            return items, "mixed:passthrough"

        groups: dict[str, list[tuple[int, Any]]] = {}
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                key = "dict"
            elif isinstance(item, str):
                key = "str"
            elif isinstance(item, bool):
                key = "bool"
            elif _is_finite_number(item):
                key = "number"
            elif isinstance(item, list):
                key = "list"
            elif item is None:
                key = "none"
            else:
                key = "other"
            groups.setdefault(key, []).append((idx, item))

        keep_indices: set[int] = set()
        strategy_parts: list[str] = []
        for key, group_items in groups.items():
            indices = [idx for idx, _ in group_items]
            values = [value for _, value in group_items]
            if len(values) < self.config.min_items_to_analyze:
                keep_indices.update(indices)
                continue
            if key == "dict":
                crushed, _strategy = self._crush_dict_array(values, query_context, bias=bias)
                crushed_hashes = {_content_hash(value) for value in crushed}
                for idx, value in group_items:
                    if _content_hash(value) in crushed_hashes:
                        keep_indices.add(idx)
                strategy_parts.append(f"dict:{len(values)}->{len(crushed)}")
            elif key == "str":
                crushed, _strategy = self._crush_string_array(values, bias=bias)
                remaining = Counter(crushed)
                for idx, value in group_items:
                    if remaining[value] > 0:
                        keep_indices.add(idx)
                        remaining[value] -= 1
                strategy_parts.append(f"str:{len(values)}->{len(crushed)}")
            elif key == "number":
                crushed, _strategy = self._crush_number_array(values, bias=bias)
                remaining = Counter(crushed)
                for idx, value in group_items:
                    if remaining[value] > 0:
                        keep_indices.add(idx)
                        remaining[value] -= 1
                strategy_parts.append(f"num:{len(values)}->{len(crushed)}")
            else:
                keep_indices.update(indices)

        result = [items[idx] for idx in sorted(keep_indices)]
        if len(result) >= n:
            return items, "mixed:passthrough"
        return result, f"mixed:adaptive({n}->{len(result)},{','.join(strategy_parts)})"

    def _crush_object(self, obj: dict[str, Any], bias: float = 1.0) -> tuple[dict[str, Any], str]:
        n = len(obj)
        if n <= 8:
            return obj, "object:passthrough"
        keys = list(obj.keys())
        kv_strings = [f"{key}:{_safe_json(obj[key])}" for key in keys]
        total_tokens = sum(len(item) for item in kv_strings) // 4
        if total_tokens < self.config.min_tokens_to_crush:
            return obj, "object:passthrough"

        k_total = compute_optimal_k(
            kv_strings,
            bias=bias,
            min_k=3,
            max_k=self.config.max_items_after_crush or None,
        )
        if k_total >= n:
            return obj, "object:passthrough"

        keep_keys: set[str] = set()
        for key, value in obj.items():
            value_text = _safe_json(value).lower()
            if any(keyword in value_text for keyword in ERROR_KEYWORDS):
                keep_keys.add(key)
            if len(value_text) <= 50:
                keep_keys.add(key)

        k_first = max(1, round(k_total * self.config.first_fraction))
        k_last = max(1, round(k_total * self.config.last_fraction))
        keep_keys.update(keys[:k_first])
        keep_keys.update(keys[-k_last:])

        remaining = max(0, k_total - len(keep_keys))
        if remaining > 0:
            stride = max(1, (n - 1) // (remaining + 1))
            for idx in range(0, n, stride):
                keep_keys.add(keys[idx])
                if len(keep_keys) >= k_total:
                    break

        result = {key: obj[key] for key in keys if key in keep_keys}
        if len(result) >= n:
            return obj, "object:passthrough"
        return result, f"object:adaptive({n}->{len(result)})"

    def _analyze_dict_array(self, items: list[dict[str, Any]]) -> ArrayAnalysis:
        field_stats: dict[str, FieldStats] = {}
        all_keys: set[str] = set()
        for item in items:
            all_keys.update(str(key) for key in item.keys())
        for key in sorted(all_keys):
            field_stats[key] = _analyze_field(key, items, self.config.variance_threshold)
        pattern = _detect_pattern(field_stats, items)
        constants = {key: stat.constant_value for key, stat in field_stats.items() if stat.is_constant}
        strategy = _select_strategy(field_stats, pattern, len(items))
        reduction = 0.0 if strategy == CompressionStrategy.SKIP else min(
            0.8, max(0.0, 1 - self.config.max_items_after_crush / max(1, len(items)))
        )
        return ArrayAnalysis(
            item_count=len(items),
            field_stats=field_stats,
            detected_pattern=pattern,
            recommended_strategy=strategy,
            constant_fields=constants,
            estimated_reduction=reduction,
        )

    def _score_field(self, analysis: ArrayAnalysis, items: list[dict[str, Any]]) -> str | None:
        best_field = None
        best_confidence = 0.0
        for name, stats in analysis.field_stats.items():
            is_score, confidence = _detect_score_field_statistically(stats, items)
            if is_score and confidence > best_confidence:
                best_field = name
                best_confidence = confidence
        return best_field

    def _numeric_anomaly_indices(
        self,
        items: list[dict[str, Any]],
        analysis: ArrayAnalysis,
    ) -> set[int]:
        out: set[int] = set()
        for name, stats in analysis.field_stats.items():
            if stats.field_type != "numeric" or stats.mean_val is None or not stats.variance:
                continue
            std = stats.variance**0.5
            if std <= 0:
                continue
            threshold = self.config.variance_threshold * std
            for idx, item in enumerate(items):
                value = item.get(name)
                if _is_finite_number(value) and abs(float(value) - stats.mean_val) > threshold:
                    out.add(idx)
        return out

    @staticmethod
    def _change_point_windows(analysis: ArrayAnalysis, total: int) -> set[int]:
        out: set[int] = set()
        for stats in analysis.field_stats.values():
            for change_point in stats.change_points:
                for offset in range(-1, 2):
                    idx = change_point + offset
                    if 0 <= idx < total:
                        out.add(idx)
        return out

    def _deduplicate_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in items:
            item_hash = _content_hash(item)
            if item_hash in seen:
                continue
            seen.add(item_hash)
            out.append(item)
        return out

    def _deduplicate_indices_by_content(
        self,
        keep_indices: set[int],
        items: list[dict[str, Any]],
    ) -> set[int]:
        if not self.config.dedup_identical_items:
            return keep_indices
        seen_hashes: dict[str, int] = {}
        for idx in sorted(keep_indices):
            if 0 <= idx < len(items):
                seen_hashes.setdefault(_content_hash(items[idx]), idx)
        return set(seen_hashes.values())

    def _fill_remaining_slots(
        self,
        keep_indices: set[int],
        items: list[dict[str, Any]],
        k_total: int,
    ) -> set[int]:
        remaining_slots = k_total - len(keep_indices)
        if remaining_slots <= 0:
            return keep_indices
        result = set(keep_indices)
        seen = {_content_hash(items[idx]) for idx in result if 0 <= idx < len(items)}
        candidates = [idx for idx in range(len(items)) if idx not in result]
        if not candidates:
            return result
        step = max(1, len(candidates) // (remaining_slots + 1))
        for start_offset in range(step):
            if len(result) >= k_total:
                break
            for pos in range(start_offset, len(candidates), step):
                idx = candidates[pos]
                item_hash = _content_hash(items[idx])
                if item_hash not in seen:
                    result.add(idx)
                    seen.add(item_hash)
                if len(result) >= k_total:
                    break
        return result

    def _prioritize_indices(
        self,
        keep_indices: set[int],
        items: list[dict[str, Any]],
        analysis: ArrayAnalysis,
        k_total: int,
    ) -> set[int]:
        critical = (
            set(_detect_error_items_for_preservation(items))
            | set(_detect_structural_outliers(items))
            | self._numeric_anomaly_indices(items, analysis)
        )
        if len(keep_indices) <= k_total:
            return keep_indices
        prioritized = set(critical)
        for idx in range(min(3, len(items))):
            if len(prioritized) >= k_total:
                break
            prioritized.add(idx)
        for idx in range(max(0, len(items) - 2), len(items)):
            if len(prioritized) >= k_total:
                break
            prioritized.add(idx)
        for idx in sorted(keep_indices):
            if len(prioritized) >= k_total:
                break
            prioritized.add(idx)
        return prioritized


def extract_query_anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    if not text:
        return anchors
    anchors.update(match.lower() for match in _UUID_PATTERN.findall(text))
    anchors.update(_NUMERIC_ID_PATTERN.findall(text))
    anchors.update(match.lower() for match in _EMAIL_PATTERN.findall(text))
    anchors.update(
        match.lower()
        for match in _HOSTNAME_PATTERN.findall(text)
        if match.lower() not in {"e.g", "i.e", "etc."}
    )
    anchors.update(
        match.strip().lower()
        for match in _QUOTED_STRING_PATTERN.findall(text)
        if len(match.strip()) >= 2
    )
    return anchors


def item_matches_anchors(item: dict[str, Any], anchors: set[str]) -> bool:
    if not anchors:
        return False
    item_text = _safe_json(item).lower()
    return any(anchor in item_text for anchor in anchors)


def _classify_array(items: list[Any]) -> ArrayType:
    if not items:
        return ArrayType.EMPTY
    types = set()
    has_bool = False
    for item in items:
        if isinstance(item, bool):
            has_bool = True
        types.add(type(item))
    if has_bool and all(isinstance(item, bool) for item in items):
        return ArrayType.BOOL_ARRAY
    if types == {dict}:
        return ArrayType.DICT_ARRAY
    if types == {str}:
        return ArrayType.STRING_ARRAY
    if all(_is_finite_number(item) for item in items) and not has_bool:
        return ArrayType.NUMBER_ARRAY
    if types == {list}:
        return ArrayType.NESTED_ARRAY
    return ArrayType.MIXED_ARRAY


def _analyze_field(key: str, items: list[dict[str, Any]], variance_threshold: float) -> FieldStats:
    values = [item.get(key) for item in items if isinstance(item, dict)]
    non_null = [value for value in values if value is not None]
    if not non_null:
        return FieldStats(key, "null", len(values), 0, 0.0, True, None)

    first = non_null[0]
    if isinstance(first, bool):
        field_type = "boolean"
    elif _is_finite_number(first):
        field_type = "numeric"
    elif isinstance(first, str):
        field_type = "string"
    elif isinstance(first, dict):
        field_type = "object"
    elif isinstance(first, list):
        field_type = "array"
    else:
        field_type = "unknown"

    str_values = [
        _safe_json(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        for value in values
    ]
    unique_values = set(str_values)
    unique_count = len(unique_values)
    unique_ratio = unique_count / len(values) if values else 0.0
    is_constant = unique_count == 1
    stats = FieldStats(
        name=key,
        field_type=field_type,
        count=len(values),
        unique_count=unique_count,
        unique_ratio=unique_ratio,
        is_constant=is_constant,
        constant_value=non_null[0] if is_constant else None,
    )

    if field_type == "numeric":
        nums = [float(value) for value in non_null if _is_finite_number(value)]
        if nums:
            stats.min_val = min(nums)
            stats.max_val = max(nums)
            stats.mean_val = statistics.mean(nums)
            stats.variance = statistics.variance(nums) if len(nums) > 1 else 0.0
            stats.change_points = _detect_change_points(nums, variance_threshold=variance_threshold)
    elif field_type == "string":
        strings = [value for value in non_null if isinstance(value, str)]
        if strings:
            stats.avg_length = statistics.mean(len(value) for value in strings)
            stats.top_values = Counter(strings).most_common(5)
    return stats


def _detect_change_points(values: list[float], *, variance_threshold: float, window: int = 5) -> list[int]:
    if len(values) < window * 2:
        return []
    overall_std = statistics.stdev(values) if len(values) > 1 else 0.0
    if overall_std == 0:
        return []
    threshold = variance_threshold * overall_std
    change_points = []
    for idx in range(window, len(values) - window):
        before = statistics.mean(values[idx - window : idx])
        after = statistics.mean(values[idx : idx + window])
        if abs(after - before) > threshold:
            change_points.append(idx)
    if not change_points:
        return []
    deduped = [change_points[0]]
    for point in change_points[1:]:
        if point - deduped[-1] > window:
            deduped.append(point)
    return deduped


def _detect_pattern(field_stats: dict[str, FieldStats], items: list[dict[str, Any]]) -> str:
    has_timestamp = any(_detect_temporal_field(name, stats, items) for name, stats in field_stats.items())
    has_numeric_variance = any(
        stats.field_type == "numeric" and stats.variance is not None and stats.variance > 0
        for stats in field_stats.values()
    )
    if has_timestamp and has_numeric_variance:
        return "time_series"
    has_high_cardinality_string = any(
        stats.field_type == "string" and stats.unique_ratio > 0.3 for stats in field_stats.values()
    )
    has_low_cardinality_category = any(
        stats.field_type in {"string", "boolean"} and 0 < stats.unique_ratio < 0.2
        for stats in field_stats.values()
    )
    if has_high_cardinality_string and has_low_cardinality_category:
        return "logs"
    if any(_detect_score_field_statistically(stats, items)[0] for stats in field_stats.values()):
        return "search_results"
    return "generic"


def _detect_temporal_field(name: str, stats: FieldStats, items: list[dict[str, Any]]) -> bool:
    if stats.field_type != "string":
        return False
    values = [item.get(name) for item in items[:30] if isinstance(item.get(name), str)]
    if not values:
        return False
    hits = sum(1 for value in values if _ISO_DATETIME_PATTERN.match(value) or _ISO_DATE_PATTERN.match(value))
    return hits / len(values) >= 0.8


def _select_strategy(
    field_stats: dict[str, FieldStats],
    pattern: str,
    item_count: int,
) -> CompressionStrategy:
    if item_count <= 8:
        return CompressionStrategy.NONE
    if pattern == "time_series":
        return CompressionStrategy.TIME_SERIES
    if pattern == "logs":
        return CompressionStrategy.CLUSTER_SAMPLE
    if pattern == "search_results":
        return CompressionStrategy.TOP_N
    string_uniqueness = [stats.unique_ratio for stats in field_stats.values() if stats.field_type == "string"]
    avg_uniqueness = statistics.mean(string_uniqueness) if string_uniqueness else 0.0
    has_numeric_signal = any(
        stats.field_type == "numeric" and stats.variance is not None and stats.variance > 0
        for stats in field_stats.values()
    )
    if avg_uniqueness > 0.92 and not has_numeric_signal:
        return CompressionStrategy.SKIP
    return CompressionStrategy.SMART_SAMPLE


def _detect_score_field_statistically(stats: FieldStats, items: list[dict[str, Any]]) -> tuple[bool, float]:
    if stats.field_type != "numeric" or stats.min_val is None or stats.max_val is None:
        return False, 0.0
    min_val = stats.min_val
    max_val = stats.max_val
    confidence = 0.0
    if 0 <= min_val <= 1 and 0 <= max_val <= 1:
        confidence += 0.4
    elif 0 <= min_val <= 10 and 0 <= max_val <= 10:
        confidence += 0.3
    elif 0 <= min_val <= 100 and 0 <= max_val <= 100:
        confidence += 0.25
    elif -1 <= min_val and max_val <= 1:
        confidence += 0.35
    else:
        return False, 0.0

    if items:
        values = [float(item[stats.name]) for item in items if _is_finite_number(item.get(stats.name))]
        if _detect_sequential_pattern(values):
            return False, 0.0
        if len(values) >= 5:
            pairs = len(values) - 1
            descending = sum(1 for idx in range(pairs) if values[idx] >= values[idx + 1])
            if pairs and descending / pairs > 0.7:
                confidence += 0.3
            float_count = sum(1 for value in values[:20] if value != int(value))
            if float_count > len(values[:20]) * 0.3:
                confidence += 0.1
    return confidence >= 0.4, min(confidence, 0.95)


def _detect_sequential_pattern(values: list[Any]) -> bool:
    nums = [float(value) for value in values if _is_finite_number(value)]
    if len(nums) < 5:
        return False
    sorted_nums = sorted(nums)
    diffs = [b - a for a, b in zip(sorted_nums, sorted_nums[1:])]
    if not diffs:
        return False
    avg_diff = sum(diffs) / len(diffs)
    if not (0.5 <= avg_diff <= 2.0):
        return False
    consistent = sum(1 for diff in diffs if 0.5 <= diff <= 2.0)
    if consistent / len(diffs) <= 0.8:
        return False
    ascending = sum(1 for a, b in zip(nums, nums[1:]) if a <= b)
    return ascending / (len(nums) - 1) > 0.7


def _detect_structural_outliers(items: list[dict[str, Any]]) -> list[int]:
    if len(items) < 3:
        return []
    field_counts: Counter[str] = Counter()
    signatures: Counter[tuple[str, ...]] = Counter()
    for item in items:
        keys = tuple(sorted(str(key) for key in item.keys()))
        signatures[keys] += 1
        field_counts.update(keys)

    n = len(items)
    common_fields = {field for field, count in field_counts.items() if count / n >= 0.7}
    rare_fields = {field for field, count in field_counts.items() if count <= max(1, round(n * 0.05))}
    common_signatures = {
        signature for signature, count in signatures.items() if count / n >= 0.1
    }

    outliers: set[int] = set()
    for idx, item in enumerate(items):
        keys = {str(key) for key in item.keys()}
        signature = tuple(sorted(keys))
        missing_common = common_fields - keys
        has_rare = bool(keys & rare_fields)
        if missing_common or has_rare or (common_signatures and signature not in common_signatures):
            outliers.add(idx)

    outliers.update(_detect_rare_status_values(items, common_fields))
    return sorted(outliers)


def _detect_rare_status_values(items: list[dict[str, Any]], common_fields: set[str]) -> list[int]:
    if len(items) < 5:
        return []
    outliers: set[int] = set()
    n = len(items)
    for field_name in sorted(common_fields):
        values = [item.get(field_name) for item in items if field_name in item]
        if len(values) < max(5, int(n * 0.5)):
            continue
        if not all(
            value is None or isinstance(value, bool | str | int)
            for value in values
        ):
            continue
        normalized = [
            _safe_json(value, sort_keys=True) if not isinstance(value, str) else value
            for value in values
        ]
        counts = Counter(normalized)
        if not (1 < len(counts) <= max(8, int(len(values) * 0.25))):
            continue
        rare_values = {
            value
            for value, count in counts.items()
            if count <= max(2, math.ceil(len(values) * 0.05))
        }
        if not rare_values:
            continue
        for idx, item in enumerate(items):
            if field_name not in item:
                continue
            normalized_value = (
                item[field_name] if isinstance(item[field_name], str)
                else _safe_json(item[field_name], sort_keys=True)
            )
            if normalized_value in rare_values:
                outliers.add(idx)
    return sorted(outliers)


def _detect_error_items_for_preservation(
    items: list[dict[str, Any]],
    item_strings: list[str] | None = None,
) -> list[int]:
    if item_strings is None:
        item_strings = [_safe_json(item, sort_keys=True) for item in items]
    out: list[int] = []
    for idx, item_text in enumerate(item_strings):
        lowered = item_text.lower()
        if any(keyword in lowered for keyword in ERROR_KEYWORDS):
            out.append(idx)
    return out


def _percentile_linear(sorted_values: list[float], q: float) -> float:
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(math.floor(pos))
    hi = min(n - 1, lo + 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value))


def _finite_float(value: Any) -> float:
    if not _is_finite_number(value):
        return float("-inf")
    return float(value)


def _safe_json(value: Any, *, sort_keys: bool = False) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=sort_keys,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return repr(value)


def _content_hash(value: Any) -> str:
    return hashlib.sha256(_safe_json(value, sort_keys=True).encode("utf-8")).hexdigest()[:16]
