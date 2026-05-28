"""Unit checks for search_journal filtering semantics."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from marginalia.agent.tools import ToolContext


def _row(note: str, tags: list[str], entry_ids: list[str] | None = None):
    return SimpleNamespace(
        id=f"j-{note}",
        conversation_id="c1",
        note=note,
        entry_ids=entry_ids or [],
        tags=tags,
        source_kind="insight",
        superseded_by_id=None,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_search_journal_tags_are_or(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = import_module("marginalia.agent.tools.search_journal")
    rows = [
        _row("alpha only", ["alpha"]),
        _row("beta only", ["beta"]),
        _row("both", ["alpha", "beta"]),
        _row("gamma only", ["gamma"]),
        _row("untagged", []),
    ]

    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        return rows

    monkeypatch.setattr(mod.journal_repo, "search", fake_search)

    result = await mod.search_journal(
        None,
        ToolContext(session_id="s1", conversation_id="c1"),
        {"tags": ["alpha", "beta"], "limit": 10},
    )

    assert [note["note"] for note in result["notes"]] == [
        "alpha only",
        "beta only",
        "both",
    ]


@pytest.mark.asyncio
async def test_search_journal_text_string_splits_to_or(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = import_module("marginalia.agent.tools.search_journal")
    rows = [
        _row("道路交通事故责任", []),
        _row("司法解释适用", []),
        _row("unrelated", []),
    ]

    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        terms = kwargs["text"]
        assert terms == ["道路交通", "法规", "赔偿", "司法解释"]
        return [
            row for row in rows
            if any(term in row.note for term in terms)
        ]

    monkeypatch.setattr(mod.journal_repo, "search", fake_search)

    result = await mod.search_journal(
        None,
        ToolContext(session_id="s1", conversation_id="c1"),
        {"text": "道路交通 法规 赔偿 司法解释", "limit": 10},
    )

    assert [note["note"] for note in result["notes"]] == [
        "道路交通事故责任",
        "司法解释适用",
    ]


@pytest.mark.asyncio
async def test_search_journal_text_array_is_or(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = import_module("marginalia.agent.tools.search_journal")
    rows = [
        _row("alpha only", []),
        _row("beta only", []),
        _row("gamma only", []),
    ]

    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        terms = kwargs["text"]
        assert terms == ["alpha", "beta"]
        return [
            row for row in rows
            if any(term in row.note for term in terms)
        ]

    monkeypatch.setattr(mod.journal_repo, "search", fake_search)

    result = await mod.search_journal(
        None,
        ToolContext(session_id="s1", conversation_id="c1"),
        {"text": ["alpha", "beta"], "limit": 10},
    )

    assert [note["note"] for note in result["notes"]] == [
        "alpha only",
        "beta only",
    ]


@pytest.mark.asyncio
async def test_search_journal_entry_id_uses_prefix_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = import_module("marginalia.agent.tools.search_journal")
    full_id = "0123abcd-1111-2222-3333-444455556666"
    rows = [
        _row("target", ["alpha"], [full_id]),
        _row("other", ["alpha"], ["9999abcd-1111-2222-3333-444455556666"]),
    ]

    async def fake_search(*args: Any, **kwargs: Any) -> list[Any]:
        return rows

    async def fake_resolve(db: Any, raw: str) -> tuple[str, str | None]:
        assert raw == "0123abcd"
        return full_id, None

    monkeypatch.setattr(mod.journal_repo, "search", fake_search)
    monkeypatch.setattr(mod.entries_repo, "resolve_entry_id_prefix", fake_resolve)

    result = await mod.search_journal(
        None,
        ToolContext(session_id="s1", conversation_id="c1"),
        {"entry_id": "0123abcd", "limit": 10},
    )

    assert [note["note"] for note in result["notes"]] == ["target"]


@pytest.mark.asyncio
async def test_search_journal_entry_id_resolution_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = import_module("marginalia.agent.tools.search_journal")

    async def fake_resolve(db: Any, raw: str) -> tuple[str, str | None]:
        return raw, "prefix is ambiguous"

    monkeypatch.setattr(mod.entries_repo, "resolve_entry_id_prefix", fake_resolve)

    result = await mod.search_journal(
        None,
        ToolContext(session_id="s1", conversation_id="c1"),
        {"entry_id": "0123abcd", "limit": 10},
    )

    assert result == {
        "notes": [],
        "count": 0,
        "has_more": False,
        "entry_id_error": "prefix is ambiguous",
    }
