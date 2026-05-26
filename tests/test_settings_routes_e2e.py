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
os.environ["LLM_DEFAULT_API_KEY"] = "sk-default-key-XXXX"
os.environ["LLM_DEFAULT_MODEL"] = "settings-default-model"
os.environ["LLM_DEFAULT_PROVIDER"] = "openai"
# Other test modules may have already exported LLM_DEFAULT_BASE_URL
# pointing at DeepSeek / a real provider — when a profile inherits the
# default api_key it'd then try to reach that host. Clear it.
os.environ.pop("LLM_DEFAULT_BASE_URL", None)


def _ensure_test_env() -> None:
    """Re-assert env at the start of each test. Other test files set
    `LLM_DEFAULT_MODEL=fake-model` at import time; in a multi-file run
    those imports fire during collection and clobber ours."""
    os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["WORKER_ENABLED"] = "false"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-default-key-XXXX"
    os.environ["LLM_DEFAULT_MODEL"] = "settings-default-model"
    os.environ["LLM_DEFAULT_PROVIDER"] = "openai"
    os.environ.pop("LLM_DEFAULT_BASE_URL", None)
    get_settings.cache_clear()  # type: ignore[attr-defined]
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
            # vision/audio inherit too but their keys must also be masked.
            assert "***" in (body["profiles"]["vision"]["api_key"] or "")
            print("[2] /v1/settings/llm: api_keys masked in GET")


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
                    },
                },
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["profiles"]["chat"]["model"] == "gpt-4o-2026"
            assert body["profiles"]["chat"]["base_url"] == "https://example.test/v1"
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
