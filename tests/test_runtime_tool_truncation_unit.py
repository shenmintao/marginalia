"""Tool-result truncation must not mutate the persisted result payload."""
from __future__ import annotations

import json

from marginalia.agent.runtime import _copy_jsonish, _structured_truncate


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
