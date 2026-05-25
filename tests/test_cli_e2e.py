"""End-to-end CLI test — exercises slash commands and chat through the CLI
client in-memory (no real HTTP server, no real LLM).

Run:
    .venv/Scripts/python tests/test_cli_e2e.py
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_cli_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.cli import CliContext, MarginaliaClient, dispatch
from marginalia.cli.commands import _ExitREPL
from marginalia.cli.render import render_markdown
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---- fake chat (so /chat fallback works without a real LLM) ----------------

class _FakeChat:
    profile_name = "chat"
    model = "fake-chat"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        # plan phase: tools=None
        if not request.tools:
            return ChatResponse(
                text="计划：直接回答。",
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=400, output_tokens=20, cache_read_tokens=300),
                parsed_json=None,
            )
        # execute phase: emit a final answer immediately, no tool calls
        return ChatResponse(
            text="# 答复\n\n这是 **agent** 的回答。",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=600, output_tokens=40, cache_read_tokens=500),
            parsed_json=None,
        )


def _install_fake_chat(client) -> None:
    import marginalia.agent.runtime as r
    r.get_chat_client = lambda profile="chat": client  # type: ignore[assignment]


async def main() -> None:
    await _create_schema()
    _install_fake_chat(_FakeChat())

    # Local file to upload
    upload_local = _TEST_ROOT / "hello.md"
    upload_local.write_text("# Hello\n\nThis is a CLI test file.\n", encoding="utf-8")

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        client = MarginaliaClient(base_url="http://t", transport=transport)
        ctx = CliContext(client=client)

        # --- 1. /help -----------------------------------------------------
        await dispatch(ctx, "/help")
        print("[1] /help OK")

        # --- 2. /upload (folder with trailing /) --------------------------
        await dispatch(ctx, f"/upload {upload_local} /docs/")
        print("[2] /upload OK")

        # --- 3. /upload (file→file via extension) -------------------------
        await dispatch(ctx, f"/upload {upload_local} /docs/renamed.md")
        print("[3] /upload renamed OK")

        # --- 4. /upload ambiguous (no ext, no slash) → server returns 400 -
        await dispatch(ctx, f"/upload {upload_local} /repos/marginalia")
        print("[4] /upload ambiguous handled (no exception raised)")

        # --- 5. /upload with --name override ------------------------------
        await dispatch(ctx, f"/upload {upload_local} /repos/marginalia --name LICENSE")
        print("[5] /upload --name OK")

        # --- 6. /tree -----------------------------------------------------
        await dispatch(ctx, "/tree")
        print("[6] /tree OK")

        # --- 7. /ls -------------------------------------------------------
        await dispatch(ctx, "/ls")
        print("[7] /ls OK")

        # --- 8. /cd then upload with relative path -----------------------
        await dispatch(ctx, "/cd /docs/")
        assert ctx.cwd_remote == "/docs/"
        await dispatch(ctx, f"/upload {upload_local} sub/")
        print("[8] /cd + relative upload OK")

        # --- 9. (the /on-conflict slash command was removed; the
        #         server-side default is now driven by config setting
        #         `default_on_conflict`. Nothing to test from the CLI.)

        # --- 10. chat (non-slash line) → opens session, runs turn --------
        assert ctx.session_id is None
        await dispatch(ctx, "你好，介绍一下自己")
        assert ctx.session_id is not None
        assert len(ctx.history) == 1
        assert "agent" in ctx.history[0]["assistant"]
        print("[10] chat OK; session_id=", ctx.session_id)

        # --- 11. /clear -> session ends; next chat opens new ----------
        prior = ctx.session_id
        await dispatch(ctx, "/clear")
        assert ctx.session_id is None
        await dispatch(ctx, "再次提问")
        assert ctx.session_id is not None and ctx.session_id != prior
        print("[11] /clear -> new session", ctx.session_id)

        # --- 12. /quit raises _ExitREPL ----------------------------------
        try:
            await dispatch(ctx, "/quit")
        except _ExitREPL:
            print("[12] /quit raises _ExitREPL")
        else:
            assert False, "/quit did not raise _ExitREPL"

        # --- 13. unknown command (graceful) ------------------------------
        await dispatch(ctx, "/no-such-command")
        print("[13] unknown command handled")

        await client.aclose()

    # --- 14. render_markdown produces ANSI escapes when supported -------
    sample = "# T\n\n**bold** and `code` and *italic*\n\n```py\nx = 1\n```"
    rendered = render_markdown(sample)
    print("[14] render_markdown sample length:", len(rendered))
    assert "T" in rendered
    assert "bold" in rendered

    print("\nALL CLI E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
