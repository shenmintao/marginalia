"""Unit checks for `format_tool_result_preview`.

The runtime feeds raw tool result dicts into this formatter and ships
the result as the user-facing `preview` string in SSE `tool_result`
events. Until this lived in tool_display.py the GUI was rendering
truncated JSON.

Run:
    .venv/Scripts/python tests/test_tool_result_preview_unit.py
"""
from __future__ import annotations

from marginalia.agent.tool_display import (
    format_tool_call,
    format_tool_result_preview,
)


def _check(name: str, result: dict, *needles: str) -> None:
    out = format_tool_result_preview(name, result)
    assert out, f"{name}: empty preview"
    assert "{" not in out, f"{name}: leaked JSON into preview: {out!r}"
    for n in needles:
        assert n in out, f"{name}: expected {n!r} in {out!r}"
    print(f"  {name:32s} -> {out}")


def test_known_shapes() -> None:
    _check("list_folder", {
        "folders": [{"id": "f1", "name": "Papers"}, {"id": "f2", "name": "Code"}],
        "count": 2,
    }, "2 folders", "Papers", "Code")

    _check("list_folder", {"folders": [], "count": 0}, "no subfolders")

    _check("list_catalogs", {
        "catalogs": [{"name": "Algorithms"}, {"name": "Systems"}],
        "count": 2,
    }, "2 catalogs", "Algorithms")

    _check("read_catalog", {
        "id": "c1", "name": "Algorithms",
        "children": [{"id": "c2", "name": "Consensus"}],
        "entries": [{"entry_id": "e1"}, {"entry_id": "e2"}],
    }, "Algorithms", "1 subcatalog", "2 entries")

    _check("read_files", {
        "ok": True,
        "results": [{
            "ok": True, "entry_id": "e1", "display_name": "raft.pdf",
            "reads": [{"text": "..."}, {"text": "..."}],
        }],
        "count": 1,
    }, "raft.pdf", "2 reads")

    _check("read_entries_metadata", {
        "entries": [{"display_name": "raft.pdf"}],
        "count": 1,
    }, "1 entry", "raft.pdf")

    _check("search_metadata", {
        "entries": [{"display_name": "raft.pdf"}, {"display_name": "paxos.pdf"}],
        "count": 2,
    }, "2 matches", "raft.pdf")

    _check("search_journal", {
        "notes": [{"note": "user asked about raft consensus algorithm"}],
        "count": 1,
    }, "1 note", "raft consensus")

    _check("resolve_tag", {
        "found": True, "name": "machine-learning", "facet": "topic",
    }, "machine-learning", "topic")

    _check("query_sql", {"rows": [{"a": 1}, {"a": 2}, {"a": 3}]}, "3 rows")
    _check("query_sql", {
        "export": {"row_count": 12, "filename": "qs_abc.csv"},
    }, "exported 12 rows", "qs_abc.csv")
    _check("query_log", {
        "operation": "count_pattern", "match_count": 4, "scanned_lines": 10,
    }, "4 matches", "10 lines")


def test_error_shape() -> None:
    out = format_tool_result_preview("read_catalog", {
        "error": "catalog not found or deleted", "id": "abc",
    })
    assert "error" in out.lower(), out
    assert "catalog not found" in out, out
    print(f"  error path                       -> {out}")


def test_list_catalogs_root_call_display() -> None:
    assert format_tool_call("list_catalogs", {}) == "list_catalogs"
    assert format_tool_call("list_catalogs", {"parent_id": None}) == "list_catalogs"
    assert format_tool_call("list_catalogs", {"parent_id": "null"}) == "list_catalogs"
    print("  list_catalogs root display       -> list_catalogs")


def test_search_text_array_call_display() -> None:
    assert format_tool_call(
        "search_metadata", {"text": ["道路交通", "法规"], "limit": 10},
    ) == 'search_metadata "道路交通" "法规" (limit 10)'
    assert format_tool_call(
        "search_journal", {"text": ["道路交通", "法规"], "limit": 10},
    ) == 'search_journal "道路交通" "法规" (limit 10)'
    assert format_tool_call(
        "search_metadata", {"text": "道路交通 法规", "limit": 10},
    ) == 'search_metadata "道路交通" "法规" (limit 10)'
    print("  text array call display          -> two quoted terms")


def test_unknown_tool_falls_back() -> None:
    out = format_tool_result_preview("brand_new_tool", {
        "count": 5, "details": []
    })
    assert "5 items" in out, out
    print(f"  unknown count path               -> {out}")

    out = format_tool_result_preview("brand_new_tool", {"foo": "bar"})
    assert "foo" in out, out
    print(f"  unknown keys path                -> {out}")


def main() -> None:
    test_known_shapes()
    test_error_shape()
    test_list_catalogs_root_call_display()
    test_search_text_array_call_display()
    test_unknown_tool_falls_back()
    print("\nALL PREVIEW-FORMATTER CHECKS PASSED")


if __name__ == "__main__":
    main()
