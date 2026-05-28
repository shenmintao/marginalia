"""Settings HTTP routes — runtime-mutable subset of `Settings`.

Three endpoints, all under `/v1/settings`:

  GET  /server      — read-only snapshot of resolved settings (no
                      secrets); the GUI uses this to render the
                      "server status" panel on the Settings page.
  GET  /llm         — per-profile resolution (chat / reflect / ingest /
                      vision / audio) with api_keys masked.
  PUT  /llm         — write a subset of LLM fields plus a few runtime
                      knobs to the overlay file. Returns the post-write
                      view so the GUI can refresh without a second GET.

Writes go through `services.config_overlay`. After a successful PUT we
clear the `get_settings()` lru_cache and the LLM client cache so the
next request sees the new values without a process restart.

Secrets are never echoed: GET masks api_keys to "sk-***" if present;
PUT accepts new api_keys but the response strips them again.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from marginalia.config import (
    LLM_PROFILES_VISIBLE,
    get_settings,
    has_vision_profile,
    resolve_profile,
)
from marginalia.llm.factory import reset_clients_cache
from marginalia.services.config_overlay import (
    OverlayValidationError, read_overlay, validate_and_normalize, write_overlay,
)

router = APIRouter(prefix="/settings", tags=["settings"])


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 6:
        return "***"
    return f"{secret[:3]}***{secret[-2:]}"


@router.get("/server")
def server_settings() -> dict[str, Any]:
    """Read-only snapshot. GUI renders this in a "Server status" card.

    No secrets, no DSNs, no S3 keys — just identifiers and toggles a
    user might want to verify. The shape is intentionally flat so the
    GUI can render it with a simple key/value list."""
    s = get_settings()
    return {
        "app_env": s.app_env,
        "marginalia_home": s.marginalia_home,
        "db_backend": s.db_backend,
        "storage_backend": s.storage_backend,
        "worker_enabled": s.worker_enabled,
        "worker_batch_size": s.worker_batch_size,
        "auto_lifecycle_enabled": s.auto_lifecycle_enabled,
        "default_on_conflict": s.default_on_conflict,
        "agent_plan_max_tokens": s.agent_plan_max_tokens,
        "agent_execute_max_tokens": s.agent_execute_max_tokens,
        "agent_execute_max_turns": s.agent_execute_max_turns,
        "agent_final_answer_continue_turns": s.agent_final_answer_continue_turns,
        "agent_final_answer_max_chars": s.agent_final_answer_max_chars,
        "llm_ingest_concurrency": s.llm_ingest_concurrency,
        "vision_profile_configured": has_vision_profile(s),
    }


@router.get("/llm")
def llm_settings() -> dict[str, Any]:
    """Per-profile resolution + the raw overlay so the GUI can show
    which fields are explicitly overridden vs inherited from defaults.

    api_keys are masked on the way out. The overlay returns the raw
    field dict (also masked) so the editor can prefill only the
    explicitly-set fields rather than every inherited value."""
    s = get_settings()
    profiles: dict[str, dict[str, Any]] = {}
    for p in LLM_PROFILES_VISIBLE:
        if p == "vision":
            # Opt-in profile: don't show the inherited default (the
            # default model is usually text-only and can't actually
            # serve vision). Reflect only what's explicitly set so an
            # unconfigured profile reads as blank in the UI.
            api_key = getattr(s, f"llm_{p}_api_key")
            profiles[p] = {
                "provider": getattr(s, f"llm_{p}_provider"),
                "api_key": _mask(api_key),
                "api_key_set": bool(api_key),
                "base_url": getattr(s, f"llm_{p}_base_url"),
                "model": getattr(s, f"llm_{p}_model"),
            }
            continue
        prof = resolve_profile(s, p)
        profiles[p] = {
            "provider": prof.provider,
            "api_key": _mask(prof.api_key),
            "api_key_set": bool(prof.api_key),
            "base_url": prof.base_url,
            "model": prof.model,
        }

    overlay = read_overlay(s.marginalia_home)
    masked_overlay: dict[str, Any] = {}
    for k, v in overlay.items():
        if k.endswith("_api_key") and isinstance(v, str):
            masked_overlay[k] = _mask(v)
        else:
            masked_overlay[k] = v

    return {
        "profiles": profiles,
        "overlay": masked_overlay,
        "defaults": {
            "provider": s.llm_default_provider,
            "model": s.llm_default_model,
            "base_url": s.llm_default_base_url,
            "api_key": _mask(s.llm_default_api_key),
            "api_key_set": bool(s.llm_default_api_key),
        },
    }


class LlmPatchBody(BaseModel):
    """Subset of overlay fields a PUT may touch.

    Sent as a flat dict because the GUI builds it from per-profile
    forms; we accept any subset of allowed fields and merge them on
    top of the existing overlay (so partial edits don't wipe other
    profiles)."""
    patch: dict[str, Any] = Field(default_factory=dict)
    # When true, replace the whole overlay with `patch` instead of
    # merging. Useful for a "reset profile" button that needs to clear
    # specific overrides — pass them as None or omit.
    replace: bool = False


@router.put("/llm")
def update_llm_settings(body: LlmPatchBody) -> dict[str, Any]:
    s = get_settings()
    try:
        clean = validate_and_normalize(body.patch)
    except OverlayValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))

    if body.replace:
        merged = clean
    else:
        merged = read_overlay(s.marginalia_home)
        # Drop keys explicitly set to None — that means "clear this
        # override" so the field falls back to .env / default.
        for k, v in clean.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v

    write_overlay(s.marginalia_home, merged)

    # Invalidate caches so the next call sees the new values.
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_clients_cache()

    return llm_settings()
