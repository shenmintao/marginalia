"""GET/PUT /v1/settings — overlay round-trip without secret echo.

The Settings page in the desktop GUI calls these to render server
status, list LLM profiles, and write back per-profile overrides. Two
behaviours we lock in here:

  1. api_keys are masked on the way out (PUT response too — never the
     raw secret).
  2. Writing an override and re-resolving the same profile reflects
     the new model/base_url, AND clears the LLM client cache so the
     next chat call uses the new config without a process restart.

Run:
    .venv/Scripts/python -m pytest tests/test_settings_routes_e2e.py -x -q
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_settings_routes_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["AUTO_LIFECYCLE_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-default-key-XXXX"
os.environ["LLM_DEFAULT_MODEL"] = "settings-default-model"
os.environ["LLM_DEFAULT_PROVIDER"] = "openai"
# Other test modules may have already exported LLM_DEFAULT_BASE_URL
# pointing at DeepSeek / a real provider — when a profile inherits the
# default api_key it'd then try to reach that host. Clear it.
os.environ.pop("LLM_DEFAULT_BASE_URL", None)
# A developer's local `.env` may pin per-profile fields like
# `LLM_INGEST_MODEL=deepseek-v4-flash`. The PUT handler does
# `get_settings.cache_clear(); llm_settings()` which re-reads `.env`
# via pydantic-settings, so popping env vars or scrubbing the cached
# instance can't survive that round-trip — we have to stop Settings
# from reading the file at all. Cut the env_file binding once at import
# time so every `Settings()` constructed during this test module sees
# only `os.environ`.
from marginalia.config import Settings as _Settings  # noqa: E402

_Settings.model_config["env_file"] = None
# Also drop any LLM_<PROFILE>_* env vars the dev's .env exported into
# os.environ (python-dotenv may have loaded them via app startup),
# so the profiles really do fall back to LLM_DEFAULT_*.
for _opt in ("CHAT", "INGEST", "REFLECT", "VISION", "AUDIO"):
    for _field in ("PROVIDER", "API_KEY", "BASE_URL", "MODEL"):
        os.environ.pop(f"LLM_{_opt}_{_field}", None)


def _scrub_optional_profiles(s) -> None:
    """Wipe vision/audio fields a developer's local .env may have set.

    Pydantic-settings reads `.env` during `Settings()` construction; env
    var pops alone can't defeat it. The "vision is opt-in / blank when
    unconfigured" contract this module locks in only holds if those
    fields are unset, so we null them on the cached instance after
    `get_settings()` returns."""
    for opt in ("vision", "audio"):
        for field in ("provider", "api_key", "base_url", "model"):
            object.__setattr__(s, f"llm_{opt}_{field}", None)


def _ensure_test_env() -> None:
    """Re-assert env at the start of each test. Other test files set
    `LLM_DEFAULT_MODEL=fake-model` at import time; in a multi-file run
    those imports fire during collection and clobber ours."""
    os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["WORKER_ENABLED"] = "false"
    os.environ["AUTO_LIFECYCLE_ENABLED"] = "false"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-default-key-XXXX"
    os.environ["LLM_DEFAULT_MODEL"] = "settings-default-model"
    os.environ["LLM_DEFAULT_PROVIDER"] = "openai"
    os.environ.pop("LLM_DEFAULT_BASE_URL", None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    _scrub_optional_profiles(get_settings())
    # Drop the GUI-write overlay too so prior tests in the same module
    # don't leak overrides into "fresh" snapshot tests.
    overlay = _TEST_ROOT / "config_overlay.json"
    if overlay.exists():
        overlay.unlink()

import httpx
from httpx import ASGITransport

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine
from marginalia.db.models import Base
from marginalia.main import app


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_server_snapshot_no_secrets() -> None:
    _ensure_test_env()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/v1/settings/server")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["storage_backend"] == "local"
            assert body["worker_enabled"] is False
            assert body["auto_lifecycle_enabled"] is False
            assert body["default_on_conflict"] in ("rename", "error", "skip")
            # Sanity: no secret keys / DSN-shaped fields snuck in.
            for k, v in body.items():
                if isinstance(v, str):
                    assert "sk-" not in v, f"{k}={v!r}"
                    assert "postgresql" not in v, f"{k}={v!r}"
            print("[1] /v1/settings/server: no secret leakage")


async def test_llm_get_masks_keys() -> None:
    _ensure_test_env()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get("/v1/settings/llm")
            assert r.status_code == 200, r.text
            body = r.json()
            assert "chat" in body["profiles"]
            chat = body["profiles"]["chat"]
            # Default api_key inherited; should be masked, not raw.
            assert chat["api_key_set"] is True
            assert chat["api_key"] != "sk-default-key-XXXX"
            assert "***" in (chat["api_key"] or "")
            # vision is opt-in: with no explicit override it should
            # read as blank, NOT fall back to the default key. The
            # Settings UI relies on this to show "(unset)" instead of
            # pretending the user inherited a usable config.
            vision = body["profiles"]["vision"]
            assert vision["api_key_set"] is False
            assert vision["api_key"] is None
            assert vision["model"] is None
            assert vision["provider"] is None
            # audio is hidden from the GUI surface until a transcription
            # pipeline lands — no `audio` key in the response.
            assert "audio" not in body["profiles"]
            print("[2] /v1/settings/llm: api_keys masked, vision blank, audio hidden")


async def test_put_writes_overlay_and_invalidates_cache() -> None:
    _ensure_test_env()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.put(
                "/v1/settings/llm",
                json={
                    "patch": {
                        "llm_chat_model": "gpt-4o-2026",
                        "llm_chat_base_url": "https://example.test/v1",
                        "agent_final_answer_continue_turns": 5,
                        "agent_final_answer_max_chars": 180000,
                    },
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["profiles"]["chat"]["model"] == "gpt-4o-2026"
            assert body["profiles"]["chat"]["base_url"] == "https://example.test/v1"
            assert body["overlay"]["agent_final_answer_continue_turns"] == 5
            assert body["overlay"]["agent_final_answer_max_chars"] == 180000
            # Other profiles still resolve from default.
            assert body["profiles"]["ingest"]["model"] == "settings-default-model"

            overlay_file = _TEST_ROOT / "config_overlay.json"
            assert overlay_file.exists(), "overlay file not created"
            disk = overlay_file.read_text(encoding="utf-8")
            assert "gpt-4o-2026" in disk
            assert "sk-default-key" not in disk, "PUT must not write defaults"

            # PUT response must not echo the raw key either.
            assert "sk-default-key-XXXX" not in r.text

            # And get_settings() now sees the new model.
            s = get_settings()
            assert s.llm_chat_model == "gpt-4o-2026"
            assert s.agent_final_answer_continue_turns == 5
            assert s.agent_final_answer_max_chars == 180000
            print("[3] PUT /v1/settings/llm: overlay written, cache invalidated")


async def test_put_clear_with_none() -> None:
    """Setting an override to null removes it from the overlay."""
    _ensure_test_env()
    # First, plant the override that test 4 will clear.
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.put(
                "/v1/settings/llm",
                json={
                    "patch": {
                        "llm_chat_model": "gpt-4o-2026",
                        "llm_chat_base_url": "https://example.test/v1",
                    },
                },
            )
            assert r.status_code == 200, r.text

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.put(
                "/v1/settings/llm",
                json={"patch": {"llm_chat_model": None}},
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["profiles"]["chat"]["model"] == "settings-default-model"
            overlay_file = _TEST_ROOT / "config_overlay.json"
            disk = overlay_file.read_text(encoding="utf-8")
            assert "llm_chat_model" not in disk
            # The other override we set in test 3 stays.
            assert "https://example.test/v1" in disk
            print("[4] PUT with null clears one override, leaves others")


async def test_put_rejects_unknown_field() -> None:
    _ensure_test_env()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.put(
                "/v1/settings/llm",
                json={"patch": {"db_backend": "postgres"}},
            )
            assert r.status_code == 422, r.text
            print("[5] PUT rejects unknown field with 422")


async def test_put_rejects_bad_provider() -> None:
    _ensure_test_env()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.put(
                "/v1/settings/llm",
                json={"patch": {"llm_chat_provider": "groq-typo"}},
            )
            assert r.status_code == 422, r.text
            print("[6] PUT rejects unknown provider with 422")


async def main() -> None:
    await _create_schema()
    await test_server_snapshot_no_secrets()
    await test_llm_get_masks_keys()
    await test_put_writes_overlay_and_invalidates_cache()
    await test_put_clear_with_none()
    await test_put_rejects_unknown_field()
    await test_put_rejects_bad_provider()
    print("\nALL SETTINGS-ROUTES TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
