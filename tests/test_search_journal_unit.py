"""Unit checks for search_journal filtering semantics."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from marginalia.agent.tools import ToolContext


def _row(note: str, tags: list[str]):
    return SimpleNamespace(
        id=f"j-{note}",
        conversation_id="c1",
        note=note,
        entry_ids=[],
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
