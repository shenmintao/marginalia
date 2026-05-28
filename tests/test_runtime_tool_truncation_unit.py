"""Tool-result truncation must not mutate the persisted result payload."""
from __future__ import annotations

import json

from marginalia.agent.runtime import _budget_tail, _copy_jsonish, _structured_truncate


def test_model_truncation_copy_preserves_original_result() -> None:
    persisted = {
        "ok": True,
        "rows": [[i, f"value-{i}"] for i in range(2000)],
    }
    original_text = json.dumps(persisted, ensure_ascii=False)

    for_model = _copy_jsonish(persisted)
    model_text, marker = _structured_truncate(for_model, 2000)

    assert marker is not None
    assert len(model_text) <= 2014  # budget + fallback suffix headroom
    assert json.dumps(persisted, ensure_ascii=False) == original_text
    assert len(persisted["rows"]) == 2000


def test_budget_tail_default_limit_matches_legacy_behavior() -> None:
    # Default limit=15: nudge previously hard-coded at turn 10 (used+1 == 11).
    early = _budget_tail(turn=0, limit=15)
    boundary_before = _budget_tail(turn=9, limit=15)
    boundary_at = _budget_tail(turn=10, limit=15)
    assert early is not None
    assert "limit 15" in early
    assert "remaining 15" in early
    assert "close to the budget limit" not in early
    assert "close to the budget limit" not in boundary_before
    assert "close to the budget limit" in boundary_at


def test_budget_tail_nudge_scales_with_limit() -> None:
    # Nudge fires once we enter the last 1/3 of the budget. Spot-check a
    # range so a future formula change can't silently regress.
    cases = [
        (6, 4),    # last 2 of 6 -> nudge from turn 4 (used+1=5 >= 5)
        (15, 10),  # legacy default
        (30, 20),
        (9, 6),
    ]
    for limit, first_nudge_turn in cases:
        before = _budget_tail(turn=first_nudge_turn - 1, limit=limit)
        at = _budget_tail(turn=first_nudge_turn, limit=limit)
        assert before is not None and at is not None
        assert "close to the budget limit" not in before, (limit, first_nudge_turn)
        assert "close to the budget limit" in at, (limit, first_nudge_turn)
        assert f"limit {limit}" in at


def test_overlay_validates_agent_execute_max_turns() -> None:
    from marginalia.services.config_overlay import (
        OverlayValidationError, validate_and_normalize,
    )

    cleaned = validate_and_normalize({"agent_execute_max_turns": "20"})
    assert cleaned == {"agent_execute_max_turns": 20}

    for bad in (2, 0, 200):
        try:
            validate_and_normalize({"agent_execute_max_turns": bad})
        except OverlayValidationError:
            continue
        raise AssertionError(f"expected validation error for {bad}")
