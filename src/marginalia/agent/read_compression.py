"""Headroom-backed compression for read_files results.

The public function remains ``compress_read_text`` so the read_files tool keeps
one stable integration point. The implementation delegates compression to
Headroom transforms and fails open to the original text whenever Headroom is not
installed, cannot compress the content, or does not beat the configured savings
threshold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from marginalia.agent.headroom_adapter import maybe_compress_read_view


@dataclass(slots=True)
class CompressionSettings:
    enabled: bool = True
    min_chars: int = 12_000
    target_chars: int = 8_000
    context_chars: int = 220
    max_ratio: float = 0.85


@dataclass(slots=True)
class ReadCompressionResult:
    text: str
    compressed: bool
    strategy: str | None = None
    original_chars: int = 0
    compressed_chars: int = 0
    omitted: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        return {
            "compressed": self.compressed,
            "strategy": self.strategy,
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "tokens_saved_estimate": max(0, self.original_chars - self.compressed_chars) // 4,
            "omitted": self.omitted,
            "lossy": bool(self.extra.get("lossy", self.compressed)),
            "quote_safe": (
                "Cite only exact text still visible in `text`; reopen the original "
                "read_files args with compress=false before quoting omitted material."
            ),
            "note": self.note,
            **self.extra,
        }


def compress_read_text(
    text: str,
    *,
    entry_id: str,
    args: dict[str, Any],
    extras: dict[str, Any] | None = None,
    pipeline: str = "",
    kind: str = "",
    query: str = "",
    settings: CompressionSettings | None = None,
) -> ReadCompressionResult:
    """Compress a read_files text result with Headroom when it is worthwhile."""
    cfg = settings or CompressionSettings()
    original_len = len(text or "")
    if not cfg.enabled or not text or original_len < cfg.min_chars:
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if args.get("compress") is False:
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if _is_precision_read(args, extras or {}):
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)

    compressed = maybe_compress_read_view(
        text,
        pipeline=pipeline,
        kind=kind,
        context=query,
        target_ratio=_target_ratio(cfg, original_len),
    )
    if compressed is None or not compressed.text.strip():
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if not _beats_threshold(
        original_chars=original_len,
        compressed_chars=len(compressed.text),
        max_ratio=cfg.max_ratio,
    ):
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)

    reopen_args = _reopen_args(args)
    omitted = [{
        "kind": "original_read",
        "entry_id": entry_id,
        "read_files_args": reopen_args,
        "original_chars": original_len,
    }]
    return ReadCompressionResult(
        text=compressed.text,
        compressed=True,
        strategy=compressed.strategy,
        original_chars=original_len,
        compressed_chars=len(compressed.text),
        omitted=omitted,
        note="Read result compressed by Headroom; reopen original args for exact omitted text.",
        extra=compressed.metadata(),
    )


def _is_precision_read(args: dict[str, Any], extras: dict[str, Any]) -> bool:
    if args.get("question") or extras.get("vlm_used"):
        return True
    if args.get("pattern") or args.get("patterns") or extras.get("hits"):
        return True
    if args.get("line_start") or args.get("line_end"):
        return True
    if args.get("paragraph_start") or args.get("paragraph_end"):
        return True
    return False


def _target_ratio(cfg: CompressionSettings, original_len: int) -> float:
    if original_len <= 0:
        return 0.5
    try:
        ratio = int(cfg.target_chars) / original_len
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = 0.5
    return min(0.8, max(0.1, ratio))


def _beats_threshold(*, original_chars: int, compressed_chars: int, max_ratio: float) -> bool:
    if original_chars <= 0:
        return False
    return compressed_chars < int(original_chars * max_ratio)


def _reopen_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "member_path",
        "offset",
        "max_chars",
        "page_start",
        "page_end",
        "page_label",
        "line_start",
        "line_end",
        "section_id",
        "heading",
        "paragraph_start",
        "paragraph_end",
    ):
        if key in args:
            out[key] = args[key]
    out["compress"] = False
    return out
