"""Persistent settings overlay — values written via the GUI / API.

`Settings()` loads from `.env` + process env once at startup. This module
adds a second, mutable layer on top: a JSON file at
`<MARGINALIA_HOME>/config_overlay.json` whose keys override matching
fields on the resolved `Settings`. The overlay is merged in
`get_settings()` so every consumer sees the same merged view.

Why a separate file instead of editing `.env`:
  - `.env` is the user's secrets file; we don't want the API to rewrite
    it (lossy on comments, may be checked in).
  - The overlay only carries the small whitelist of fields that make
    sense to change at runtime — LLM profiles, conflict policy, agent
    token budgets, worker concurrency, and bounded LLM ingest fan-out.
    Storage backend, db, and most worker cadence still need a restart
    and stay in `.env`.

Writes are atomic (tmp + rename). The file is created on first PUT.
After a successful write, callers must invalidate the
`get_settings()` lru_cache and any `lru_cache`d clients (factory).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Whitelist: only these field names may live in the overlay. Anything
# else gets dropped silently on read and rejected on write — keeps the
# blast radius small if the file is hand-edited or corrupted.
_ALLOWED_FIELDS: frozenset[str] = frozenset({
    "default_on_conflict",
    "agent_plan_max_tokens",
    "agent_execute_max_tokens",
    "agent_execute_max_turns",
    "agent_final_answer_continue_turns",
    "agent_final_answer_max_chars",
    "llm_ingest_concurrency",
    "worker_batch_size",
    # LLM defaults
    "llm_default_provider",
    "llm_default_api_key",
    "llm_default_base_url",
    "llm_default_model",
    # Per-profile overrides
    "llm_chat_provider", "llm_chat_api_key", "llm_chat_base_url", "llm_chat_model",
    "llm_reflect_provider", "llm_reflect_api_key", "llm_reflect_base_url", "llm_reflect_model",
    "llm_ingest_provider", "llm_ingest_api_key", "llm_ingest_base_url", "llm_ingest_model",
    "llm_vision_provider", "llm_vision_api_key", "llm_vision_base_url", "llm_vision_model",
    # llm_audio_* fields are intentionally NOT in the allowlist: no
    # pipeline consumes the audio profile yet, so accepting writes
    # would just persist dead config that misleads the user when
    # nothing happens. Re-add when a transcription pipeline lands.
})

_VALID_PROVIDERS: frozenset[str] = frozenset({"openai", "openai-compatible", "anthropic"})
_VALID_CONFLICT: frozenset[str] = frozenset({"rename", "error", "skip"})


def overlay_path(home: str | os.PathLike[str]) -> Path:
    return Path(home) / "config_overlay.json"


def read_overlay(home: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the on-disk overlay, filtered to the allowed fields.

    Missing file → empty dict. Malformed JSON → empty dict (we don't
    want a typo in the overlay to brick the whole app)."""
    p = overlay_path(home)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if k in _ALLOWED_FIELDS}


def write_overlay(home: str | os.PathLike[str], values: dict[str, Any]) -> None:
    """Replace the overlay file with `values` (already validated).

    Atomic: write to a tmp file in the same directory, then `os.replace`
    so a half-written JSON never appears."""
    p = overlay_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".config_overlay.", suffix=".json", dir=str(p.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(values, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_name, p)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class OverlayValidationError(ValueError):
    """One or more fields in a PUT body failed validation."""


def validate_and_normalize(patch: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown fields and bad enum values; coerce ints; turn
    blank strings into ``None`` (so a profile override truly clears).

    Returns the cleaned dict ready to merge into the existing overlay.
    """
    out: dict[str, Any] = {}
    bad: list[str] = []
    for k, v in patch.items():
        if k not in _ALLOWED_FIELDS:
            bad.append(f"{k}: unknown field")
            continue
        if v == "":
            v = None
        if k.endswith("_provider") and v is not None:
            if v not in _VALID_PROVIDERS:
                bad.append(f"{k}: must be one of {sorted(_VALID_PROVIDERS)}")
                continue
        if k == "default_on_conflict":
            if v not in _VALID_CONFLICT:
                bad.append(f"{k}: must be one of {sorted(_VALID_CONFLICT)}")
                continue
        if k in (
            "agent_plan_max_tokens",
            "agent_execute_max_tokens",
            "agent_execute_max_turns",
            "agent_final_answer_continue_turns",
            "agent_final_answer_max_chars",
            "llm_ingest_concurrency",
            "worker_batch_size",
        ):
            try:
                v = int(v)
            except (TypeError, ValueError):
                bad.append(f"{k}: must be an integer")
                continue
            lower = 0 if k == "agent_final_answer_continue_turns" else 1
            if k == "agent_execute_max_turns":
                lower, upper = 3, 100
            elif k in ("llm_ingest_concurrency", "worker_batch_size"):
                upper = 32
            else:
                upper = 200000
            if v < lower or v > upper:
                bad.append(f"{k}: out of range [{lower}, {upper}]")
                continue
        out[k] = v
    if bad:
        raise OverlayValidationError("; ".join(bad))
    return out


def merge_overlay_into_settings(settings: Any, overlay: dict[str, Any]) -> None:
    """Apply `overlay` onto `settings` in place.

    Pydantic v2 `BaseSettings` instances are mutable by default unless
    frozen; `Settings` here is not frozen so attribute assignment works.
    Done in place so the lru_cache result holds the merged view.
    """
    for k, v in overlay.items():
        if hasattr(settings, k):
            setattr(settings, k, v)
