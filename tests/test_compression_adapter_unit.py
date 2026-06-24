from __future__ import annotations

from types import SimpleNamespace

from marginalia.agent import compression_adapter as mod


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
        lambda tool_name, payload, context: mod.CompressedText(
            text="a\n1\n2",
            strategy="headroom.smart_crusher.records",
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
    assert result["compressed_for_model"] is True
    assert result["columns"] == ["a", "b"]
    assert result["row_count"] == 50
    assert result["compressed_text"] == "a\n1\n2"
    assert result["compression"]["strategy"] == "headroom.smart_crusher.records"


def test_ingest_compression_records_metadata(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings())
    monkeypatch.setattr(
        mod,
        "_compress_ingest_text",
        lambda body, kind, context: mod.CompressedText(
            text="ERROR compact",
            strategy="headroom.log_compressor",
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
    assert meta["strategy"] == "headroom.log_compressor"
    assert meta["lossy"] is True


def test_non_query_tool_is_not_compressed_for_model(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings())

    result = mod.maybe_compress_tool_result_for_model(
        "read_files",
        {"ok": True, "text": "x" * 1000},
    )

    assert result is None


def test_read_routing_uses_source_and_member_extensions() -> None:
    assert mod._read_route(
        "not actually json",
        pipeline="text",
        kind="text",
        source_name="data.jsonl",
    ) == "json"
    assert mod._read_route(
        "name | total\na | 1",
        pipeline="text",
        kind="text",
        member_path="reports/sheet.csv",
    ) == "table"
    assert mod._read_route(
        "plain text",
        pipeline="text",
        kind="text",
        source_name="src/worker.py",
    ) == "code"


def test_read_code_route_is_not_compressed_without_vendored_code_compressor() -> None:
    skipped = mod._compress_read_text(
        "def fn():\n    return 1\n",
        pipeline="text",
        kind="text",
        context="",
        target_ratio=0.5,
        source_name="worker.py",
        allow_code=False,
    )
    explicit = mod._compress_read_text(
        "def fn():\n    return 1\n",
        pipeline="text",
        kind="text",
        context="",
        target_ratio=0.5,
        source_name="worker.py",
        allow_code=True,
    )

    assert skipped is None
    assert explicit is None


def test_table_read_route_builds_records_for_smartcrusher(monkeypatch) -> None:
    calls = []

    def fake_records(records, *, context, original_chars=None, source_format="records", lossy=False):
        calls.append({
            "records": records,
            "context": context,
            "original_chars": original_chars,
            "source_format": source_format,
            "lossy": lossy,
        })
        return mod.CompressedText(
            text="compact table",
            strategy="headroom.smart_crusher.table",
            original_chars=original_chars or 0,
            compressed_chars=13,
            extra={"lossy": lossy},
        )

    monkeypatch.setattr(mod, "_compress_records", fake_records)
    body = "# Sheet: Main\nname | total\nalpha | 3"

    compressed = mod._compress_read_text(
        body,
        pipeline="spreadsheet",
        kind="table",
        context="totals",
        target_ratio=0.5,
    )

    assert compressed is not None
    assert compressed.text == "compact table"
    assert compressed.extra["route"] == "table"
    assert calls[0]["source_format"] == "table-text"
    assert calls[0]["lossy"] is True
    assert calls[0]["records"] == [
        {"row": 1, "sheet": "Main", "name": "alpha", "total": "3"}
    ]


def test_jsonl_read_route_uses_records(monkeypatch) -> None:
    calls = []

    def fake_records(records, *, context, original_chars=None, source_format="records", lossy=False):
        calls.append({"records": records, "source_format": source_format})
        return mod.CompressedText(
            text="compact jsonl",
            strategy="headroom.smart_crusher.json",
            original_chars=original_chars or 0,
            compressed_chars=13,
            extra={"lossy": lossy},
        )

    monkeypatch.setattr(mod, "_compress_records", fake_records)
    compressed = mod._compress_read_text(
        '{"a": 1}\n{"a": 2}',
        pipeline="text",
        kind="text",
        context="",
        target_ratio=0.5,
        source_name="events.jsonl",
    )

    assert compressed is not None
    assert compressed.extra["route"] == "json"
    assert calls == [{"records": [{"a": 1}, {"a": 2}], "source_format": "jsonl"}]


def test_ingest_table_routes_to_table_compressor(monkeypatch) -> None:
    monkeypatch.setattr(
        mod,
        "_compress_table_text",
        lambda body, context: mod.CompressedText(
            text="compact table",
            strategy="headroom.smart_crusher.table",
            original_chars=len(body),
            compressed_chars=13,
            extra={"lossy": True},
        ),
    )

    compressed = mod._compress_ingest_text(
        "name | total\nalpha | 3",
        kind="table",
        context="sheet.xlsx",
    )

    assert compressed is not None
    assert compressed.text == "compact table"
    assert compressed.strategy == "headroom.smart_crusher.table"


def test_ingest_jsonl_routes_to_structured_compressor(monkeypatch) -> None:
    calls = []

    def fake_records(records, *, context, original_chars=None, source_format="records", lossy=False):
        calls.append({"records": records, "source_format": source_format})
        return mod.CompressedText(
            text="compact jsonl",
            strategy="headroom.smart_crusher.json",
            original_chars=original_chars or 0,
            compressed_chars=13,
            extra={"lossy": lossy},
        )

    monkeypatch.setattr(mod, "_compress_records", fake_records)
    compressed = mod._compress_ingest_text(
        '{"event": "a"}\n{"event": "b"}',
        kind="text",
        context="events.jsonl",
    )

    assert compressed is not None
    assert compressed.text == "compact jsonl"
    assert calls == [
        {"records": [{"event": "a"}, {"event": "b"}], "source_format": "jsonl"}
    ]


def test_ingest_plain_text_does_not_compress_raw_text(monkeypatch) -> None:
    def fail_plain(*args, **kwargs) -> None:
        raise AssertionError("plain text ingest should only compress aggregate maps")

    monkeypatch.setattr(mod, "_compress_plain_text", fail_plain)
    body = "ordinary paragraph\n" * 2000

    compressed = mod._compress_ingest_text(body, kind="text", context="notes.md")

    assert compressed is None


def test_ingest_text_does_not_compress_code_by_default(monkeypatch) -> None:
    def fail_plain(*args, **kwargs) -> None:
        raise AssertionError("code-shaped ingest should not use text compression")

    monkeypatch.setattr(mod, "_compress_plain_text", fail_plain)
    body = "def fn():\n    return 1\n" * 2000

    compressed = mod._compress_ingest_text(body, kind="text", context="worker.py")

    assert compressed is None


def test_archive_peeks_are_compressed_with_reopen_metadata(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings(compression_min_chars=1))
    monkeypatch.setattr(
        mod,
        "_compress_read_text",
        lambda body, **kwargs: mod.CompressedText(
            text="compact peek",
            strategy="headroom.log_compressor",
            original_chars=len(body),
            compressed_chars=12,
            extra={"lossy": True, "route": "log"},
        ),
    )

    out = mod.maybe_compress_archive_peeks([
        {"path": "logs/access.log", "kind": "log", "preview": "ERROR noisy line\n" * 50}
    ], context="bundle.zip")

    assert out[0]["preview"] == "compact peek"
    assert out[0]["compression"]["route"] == "log"
    assert out[0]["compression"]["reopen"] == {
        "member_path": "logs/access.log",
        "compress": False,
    }


def test_ingest_aggregate_view_uses_text_compressor(monkeypatch) -> None:
    monkeypatch.setattr(mod, "get_settings", lambda: _settings(compression_min_chars=1))
    monkeypatch.setattr(
        mod,
        "_compress_plain_text",
        lambda body, context, target_ratio: mod.CompressedText(
            text="compact aggregate",
            strategy="headroom.text_crusher",
            original_chars=len(body),
            compressed_chars=17,
            extra={"lossy": True},
        ),
    )

    text, meta = mod.maybe_compress_ingest_aggregate_view(
        "section map\n" * 100,
        kind="text_aggregate",
        context="long.md",
    )

    assert text == "compact aggregate"
    assert meta is not None
    assert meta["aggregate"] is True
    assert meta["kind"] == "text_aggregate"


def test_actual_headroom_log_compressor_preserves_error_lines() -> None:
    body = "\n".join(
        [f"2026-01-01T00:00:{idx:02d}Z INFO worker heartbeat {idx}" for idx in range(90)]
        + ["2026-01-01T00:02:00Z ERROR payment failed with timeout"]
        + [f"2026-01-01T00:03:{idx:02d}Z INFO worker recovered {idx}" for idx in range(90)]
    )

    compressed = mod._compress_log_text(body, context="payment timeout")

    assert compressed is not None
    assert compressed.strategy == "headroom.log_compressor"
    assert "ERROR payment failed" in compressed.text
    assert compressed.extra["lines_omitted"] > 0
    assert len(compressed.text) < len(body)


def test_actual_headroom_search_compressor_omits_redundant_matches() -> None:
    body = "\n".join(
        f"src/service_{idx % 4}.py:{idx + 1}:def handler_{idx}(): raise RuntimeError('boom')"
        for idx in range(60)
    )

    compressed = mod._compress_search_text(body, context="RuntimeError service_1")

    assert compressed is not None
    assert compressed.strategy == "headroom.search_compressor"
    assert compressed.extra["matches_omitted"] > 0
    assert "more matches" in compressed.text
    assert len(compressed.text) < len(body)


def test_actual_headroom_smartcrusher_preserves_error_record() -> None:
    records = [
        {"id": idx, "status": "ok", "score": 100 - idx, "message": "routine event"}
        for idx in range(80)
    ]
    records[57] = {
        "id": 57,
        "status": "failed",
        "score": 0,
        "message": "critical failure in ingest worker",
    }

    compressed = mod._compress_records(records, context="critical failure")

    assert compressed is not None
    assert compressed.strategy == "headroom.smart_crusher.records"
    assert "critical failure" in compressed.text
    assert compressed.extra["compressed_item_count"] < len(records)
    assert len(compressed.text) < compressed.original_chars


def test_actual_headroom_textcrusher_compresses_plain_text() -> None:
    body = "\n".join(
        f"Section {idx}. The ingest aggregate mentions keyword-{idx} and stable supporting detail."
        for idx in range(120)
    )

    compressed = mod._compress_plain_text(body, context="keyword-119", target_ratio=0.25)

    assert compressed is not None
    assert compressed.strategy == "headroom.text_crusher"
    assert compressed.extra["kept_segments"] < compressed.extra["total_segments"]
    assert len(compressed.text) < len(body)
