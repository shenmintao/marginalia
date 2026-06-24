from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marginalia.agent import read_compression as mod
from marginalia.agent.read_compression import (
    CompressionSettings,
    compress_read_text,
)
from marginalia.pipelines import resolve_pipeline


@dataclass(slots=True)
class FakeCompressed:
    text: str = "compact headroom view"
    strategy: str = "headroom.fake"
    lossy: bool = True

    def metadata(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "original_chars": 1000,
            "compressed_chars": len(self.text),
            "tokens_saved_estimate": 100,
            "lossy": self.lossy,
        }


def _cfg(**overrides: Any) -> CompressionSettings:
    values = dict(
        enabled=True,
        min_chars=10,
        target_chars=100,
        context_chars=40,
        max_ratio=0.85,
    )
    values.update(overrides)
    return CompressionSettings(**values)


def test_disabled_or_small_reads_are_not_compressed(monkeypatch) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Headroom should not be called")

    monkeypatch.setattr(mod, "maybe_compress_read_view", fail_if_called)

    disabled = compress_read_text(
        "x" * 100,
        entry_id="entry-text",
        args={"max_chars": 1000},
        settings=_cfg(enabled=False),
    )
    small = compress_read_text(
        "x" * 9,
        entry_id="entry-text",
        args={"max_chars": 1000},
        settings=_cfg(min_chars=10),
    )

    assert disabled.compressed is False
    assert small.compressed is False


def test_precision_reads_are_not_compressed(monkeypatch) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Headroom should not be called")

    monkeypatch.setattr(mod, "maybe_compress_read_view", fail_if_called)
    text = "needle\n" + ("large context\n" * 20)

    result = compress_read_text(
        text,
        entry_id="entry-pattern",
        args={"pattern": "needle"},
        extras={"hits": [{"line": 1}]},
        pipeline="text",
        query="needle",
        settings=_cfg(),
    )

    assert result.compressed is False
    assert result.text == text


def test_explicit_uncompressed_read_is_not_recompressed(monkeypatch) -> None:
    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Headroom should not be called")

    monkeypatch.setattr(mod, "maybe_compress_read_view", fail_if_called)
    text = "x" * 100

    result = compress_read_text(
        text,
        entry_id="entry-text",
        args={"compress": False, "max_chars": 1000},
        settings=_cfg(),
    )

    assert result.compressed is False
    assert result.text == text


def test_successful_headroom_compression_returns_reopen_args(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_compress(
        body: str,
        *,
        pipeline: str,
        kind: str,
        context: str,
        target_ratio: float,
    ) -> FakeCompressed:
        calls.append({
            "body": body,
            "pipeline": pipeline,
            "kind": kind,
            "context": context,
            "target_ratio": target_ratio,
        })
        return FakeCompressed()

    monkeypatch.setattr(mod, "maybe_compress_read_view", fake_compress)
    text = "0123456789" * 100

    result = compress_read_text(
        text,
        entry_id="entry-text",
        args={"member_path": "chapter.md", "max_chars": 12000, "offset": 200},
        pipeline="text",
        kind="text",
        query="target signal",
        settings=_cfg(target_chars=250),
    )

    assert result.compressed is True
    assert result.text == "compact headroom view"
    assert result.strategy == "headroom.fake"
    assert calls == [
        {
            "body": text,
            "pipeline": "text",
            "kind": "text",
            "context": "target signal",
            "target_ratio": 0.25,
        }
    ]
    assert result.omitted == [
        {
            "kind": "original_read",
            "entry_id": "entry-text",
            "read_files_args": {
                "member_path": "chapter.md",
                "offset": 200,
                "max_chars": 12000,
                "compress": False,
            },
            "original_chars": len(text),
        }
    ]
    meta = result.metadata()
    assert meta["compressed"] is True
    assert meta["lossy"] is True
    assert meta["quote_safe"]


def test_weak_headroom_compression_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "maybe_compress_read_view",
        lambda *args, **kwargs: FakeCompressed(text="y" * 90),
    )
    text = "x" * 100

    result = compress_read_text(
        text,
        entry_id="entry-text",
        args={"max_chars": 1000},
        settings=_cfg(max_ratio=0.85),
    )

    assert result.compressed is False
    assert result.text == text


def test_headroom_none_fails_open(monkeypatch) -> None:
    monkeypatch.setattr(mod, "maybe_compress_read_view", lambda *args, **kwargs: None)
    text = "x" * 100

    result = compress_read_text(
        text,
        entry_id="entry-text",
        args={"max_chars": 1000},
        settings=_cfg(),
    )

    assert result.compressed is False
    assert result.text == text


def test_text_pipeline_routes_json_and_code_extensions() -> None:
    assert resolve_pipeline("application/json", ".json", filename="data.json").name == "text"
    assert resolve_pipeline(None, ".py", filename="worker.py").name == "text"
