from __future__ import annotations

from types import SimpleNamespace

from marginalia.agent import headroom_adapter as mod


def _settings(**overrides):
    base = dict(
        compression_enabled=True,
        compression_min_chars=1,
        compression_max_ratio=0.9,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_query_tool_compression_is_disabled_by_default_gate(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "get_settings",
        lambda: _settings(compression_enabled=False),
    )

    result = mod.maybe_compress_tool_result_for_model(
        "query_sql",
        {"ok": True, "columns": ["a"], "rows": [[1], [2]]},
    )

    assert result is None


def test_query_tool_compression_returns_model_only_envelope(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        mod,
        "_compress_query_payload",
        lambda tool_name, payload, context: mod.HeadroomText(
            text="a\n1\n2",
            strategy="headroom.smart_crusher.csv-schema",
            original_chars=200,
            compressed_chars=5,
            extra={"lossy": False},
        ),
    )

    payload = {
        "ok": True,
        "columns": ["a", "b"],
        "rows": [[idx, "value" * 200] for idx in range(50)],
        "row_count": 50,
    }
    result = mod.maybe_compress_tool_result_for_model(
        "query_sql",
        payload,
        context="numbers",
    )

    assert result is not None
    assert result["headroom_compressed"] is True
    assert result["columns"] == ["a", "b"]
    assert result["row_count"] == 50
    assert result["compressed_text"] == "a\n1\n2"
    assert result["compression"]["strategy"] == "headroom.smart_crusher.csv-schema"


def test_ingest_compression_records_metadata(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        mod,
        "_compress_ingest_text",
        lambda body, kind, context: mod.HeadroomText(
            text="ERROR compact",
            strategy="headroom.log",
            original_chars=500,
            compressed_chars=13,
            extra={"lossy": True},
        ),
    )

    text, meta = mod.maybe_compress_ingest_view(
        "INFO noise\n" * 50,
        kind="log",
        context="server.log",
    )

    assert text == "ERROR compact"
    assert meta is not None
    assert meta["strategy"] == "headroom.log"
    assert meta["lossy"] is True


def test_non_query_tool_is_not_headroom_compressed(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings())

    result = mod.maybe_compress_tool_result_for_model(
        "read_files",
        {"ok": True, "text": "x" * 1000},
    )

    assert result is None
