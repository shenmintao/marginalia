"""End-to-end tests for the CLI upgrade cycle:

  (A) prompt_toolkit-backed REPL — pipe-mode fallback still works.
  (B) Spinner + table rendering in render.py.
  (C) `marginalia init` bootstrap command.

No real HTTP server, no real LLM, no TTY required.

Run:
    .venv/Scripts/python tests/test_cli_upgrade_e2e.py
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import time
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_cli_upgrade_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)

# Force the spinner / colour code into a deterministic OFF state so the
# test runs identically on TTY and non-TTY.  The Spinner itself also has a
# runtime check, but we set TERM=dumb to belt-and-brace.
os.environ["TERM"] = "dumb"
os.environ.pop("NO_COLOR", None)


# ---- (B) Spinner --------------------------------------------------------------

def test_spinner_no_op_when_not_tty() -> None:
    """Spinner is silent when stdout is not a TTY (it must not write frames)."""
    from marginalia.cli.render import Spinner

    buf = io.StringIO()
    real_stdout = sys.stdout
    try:
        sys.stdout = buf
        sp = Spinner("working...").start()
        # Disabled spinners should not start a thread:
        assert sp._thread is None, "spinner should not animate when not TTY"
        sp.update("changed")
        sp.finish("done")
        sp.fail("oops")
    finally:
        sys.stdout = real_stdout

    output = buf.getvalue()
    # nothing should have been written when disabled
    assert output == "", f"expected no output, got {output!r}"
    print("[B1] Spinner is no-op when not TTY")


def test_spinner_context_manager_runs_without_error() -> None:
    """Spinner used as a context manager must not raise even when disabled."""
    from marginalia.cli.render import Spinner, spinner

    with Spinner("ctx test"):
        time.sleep(0.01)
    with spinner("functional ctx"):
        time.sleep(0.01)
    print("[B2] Spinner context-manager paths run cleanly")


def test_spinner_animates_when_forced_enabled() -> None:
    """Smoke test the animation loop by force-enabling and joining quickly.

    We don't assert on exact frames (timing flake), just that some output
    landed and the thread shut down cleanly."""
    from marginalia.cli.render import Spinner

    buf = io.StringIO()
    real_stdout = sys.stdout
    try:
        sys.stdout = buf
        sp = Spinner("forced")
        sp._enabled = True  # bypass TTY gate for this test only
        sp.start()
        time.sleep(0.18)
        sp.finish("done")
        # thread should be cleaned up after finish
        assert sp._thread is None
    finally:
        sys.stdout = real_stdout

    output = buf.getvalue()
    assert "done" in output, f"finish marker missing: {output!r}"
    assert "✓" in output, f"success glyph missing: {output!r}"
    print("[B3] Spinner animates + clears when forced TTY")


# ---- (B) Table rendering ------------------------------------------------------

def test_render_table_alignment() -> None:
    """`render_markdown` should detect a `| a | b |` block and emit aligned rows."""
    from marginalia.cli.render import render_markdown

    md = (
        "前言。\n"
        "\n"
        "| name | age |\n"
        "| ---- | --- |\n"
        "| Aria | 30  |\n"
        "| Bo   | 7   |\n"
        "\n"
        "结尾。\n"
    )
    out = render_markdown(md)

    # All cell contents should appear and be on table lines (separated by │)
    assert "name" in out
    assert "Aria" in out
    assert "Bo" in out
    # the separator row (`| --- | --- |`) must NOT appear verbatim in output
    assert "----" not in out, "separator row leaked into rendering"
    # vertical bar chars are emitted
    assert "│" in out, "table border vertical bar missing"
    # horizontal border line appears at least twice (top + bottom)
    border_lines = [ln for ln in out.splitlines() if "─" in ln]
    assert len(border_lines) >= 2, f"expected borders, got: {border_lines!r}"
    # the prose around the table is preserved
    assert "前言。" in out
    assert "结尾。" in out
    print("[B4] render_markdown table alignment OK")


def test_render_table_no_header_separator() -> None:
    """Tables without a `---` row should still render (no special header)."""
    from marginalia.cli.render import render_table

    out = render_table(["| a | bb |", "| ccc | d |"])
    assert "a" in out and "bb" in out and "ccc" in out and "d" in out
    print("[B5] render_table without header separator OK")


# ---- (C) marginalia init -----------------------------------------------------

def test_init_project_creates_artifacts() -> None:
    """`init_project` creates .env / data/ / .marginalia/ / .gitignore."""
    from marginalia.cli.init_cmd import init_project, _Status

    tgt = _TEST_ROOT / "init_fresh"
    tgt.mkdir(parents=True)
    artifacts = init_project(tgt)

    names = {a.name: a.status for a in artifacts}
    assert ".env" in names and names[".env"] == _Status.CREATED
    assert "data/" in names and names["data/"] == _Status.CREATED
    assert "data/library/" in names and names["data/library/"] == _Status.CREATED
    assert ".marginalia/" in names and names[".marginalia/"] == _Status.CREATED
    assert ".gitignore" in names and names[".gitignore"] == _Status.CREATED

    # File system reality checks
    assert (tgt / ".env").is_file()
    assert (tgt / "data").is_dir()
    assert (tgt / "data" / "library").is_dir()
    assert (tgt / ".marginalia").is_dir()
    gi = (tgt / ".gitignore").read_text(encoding="utf-8")
    for entry in (".env", "data/", ".marginalia/", "*.db", "*.db-shm", "*.db-wal"):
        assert entry in gi, f"missing entry {entry!r} in .gitignore"
    env = (tgt / ".env").read_text(encoding="utf-8")
    assert "DB_BACKEND" in env
    assert "STORAGE_BACKEND" in env
    assert "LLM_DEFAULT_API_KEY" in env

    print("[C1] init_project bootstrap files created")


def test_init_project_idempotent() -> None:
    """Running init twice should report SKIPPED, not overwrite, not error."""
    from marginalia.cli.init_cmd import init_project, _Status

    tgt = _TEST_ROOT / "init_fresh"  # reuse the directory from the previous test
    artifacts = init_project(tgt)
    statuses = {a.name: a.status for a in artifacts}
    # Everything already exists → SKIPPED
    for n in (".env", "data/", "data/library/", ".marginalia/"):
        assert statuses[n] == _Status.SKIPPED, f"{n} not skipped on rerun"
    # .gitignore: all entries present → SKIPPED (not UPDATED)
    assert statuses[".gitignore"] == _Status.SKIPPED
    print("[C2] init_project idempotent")


def test_init_project_appends_existing_gitignore() -> None:
    """When .gitignore exists with unrelated content, init should append our
    entries without removing existing lines."""
    from marginalia.cli.init_cmd import init_project, _Status

    tgt = _TEST_ROOT / "init_pre_existing_gitignore"
    tgt.mkdir(parents=True)
    pre = "node_modules/\n*.log\n"
    (tgt / ".gitignore").write_text(pre, encoding="utf-8")

    artifacts = init_project(tgt)
    statuses = {a.name: a.status for a in artifacts}
    assert statuses[".gitignore"] == _Status.UPDATED

    body = (tgt / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in body, "pre-existing entry was clobbered"
    assert "*.log" in body
    assert ".env" in body and "data/" in body
    print("[C3] init_project preserves existing .gitignore content")


def test_init_render_report() -> None:
    """`render_report` returns a multi-line string mentioning each artifact."""
    from marginalia.cli.init_cmd import init_project, render_report

    tgt = _TEST_ROOT / "init_render_report"
    tgt.mkdir(parents=True)
    artifacts = init_project(tgt)
    text = render_report(tgt, artifacts)

    assert "marginalia init" in text
    assert ".env" in text
    assert "data/" in text
    assert ".gitignore" in text
    assert "Next steps" in text
    print("[C4] render_report renders all artifacts")


# ---- (A) prompt_toolkit REPL — fallback path ---------------------------------

def test_repl_module_imports_and_main_dispatches_init() -> None:
    """`marginalia init` subcommand must be detected in main() before argparse,
    so it does not collide with --server. Run main() with argv replaced to
    confirm it returns 0 and creates the bootstrap files."""
    from marginalia.cli import repl

    tgt = _TEST_ROOT / "init_via_main"
    tgt.mkdir(parents=True)

    real_argv = sys.argv
    try:
        sys.argv = ["marginalia", "init", str(tgt)]
        rc = repl.main()
    finally:
        sys.argv = real_argv

    assert rc == 0
    assert (tgt / ".env").is_file()
    assert (tgt / "data").is_dir()
    print("[A1] `marginalia init` dispatched through repl.main() OK")


def test_repl_pt_session_buildable() -> None:
    """The slash completer wired into the prompt_toolkit session must yield
    `/help` (and friends) when the user types `/`. We test the completer
    directly because building a full PromptSession on Windows requires a
    real console buffer, which is unavailable under piped test output."""
    from marginalia.cli.repl import _make_slash_completer

    # Ensure the command registry is populated
    from marginalia.cli import commands as _cmds  # noqa: F401
    from prompt_toolkit.document import Document

    completer = _make_slash_completer()
    completions = list(completer.get_completions(
        Document(text="/he", cursor_position=3),
        complete_event=None,
    ))
    labels = {c.text for c in completions}
    assert "/help" in labels, f"slash completer missing /help, got: {labels!r}"

    # And typing a non-slash prefix yields nothing
    no_completions = list(completer.get_completions(
        Document(text="hello", cursor_position=5),
        complete_event=None,
    ))
    assert no_completions == [], "completer should be silent on non-slash input"
    print("[A2] prompt_toolkit slash completer wired correctly")


def test_repl_fallback_when_not_tty() -> None:
    """When stdin is not a TTY, run_repl() must use the readline fallback path
    and remain compatible with the existing piped-input contract.

    This is a unit-level check: we patch the readers to capture which one
    is selected, then exercise run_repl with ASGITransport.

    NOTE: requires DB env vars from earlier tests; isolated DB so we don't
    pollute test_cli_e2e's state."""
    import asyncio
    import httpx
    from httpx import ASGITransport

    # Carve a fresh DB sandbox just for this test
    sandbox = _TEST_ROOT / "repl_fallback"
    sandbox.mkdir(parents=True)
    os.environ["MARGINALIA_HOME"] = str(sandbox)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["WORKER_ENABLED"] = "false"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
    os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

    from marginalia.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from marginalia.db.engine import get_engine
    from marginalia.db.models import Base
    from marginalia.main import app
    from marginalia.cli import repl as repl_mod

    async def _setup_schema() -> None:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup_schema())

    # Track which reader was invoked.
    selected = {"used_pt": None}

    async def _fake_pt(_session):
        selected["used_pt"] = True
        return None  # immediate EOF -> exit loop

    async def _fake_stdin():
        selected["used_pt"] = False
        return None  # immediate EOF -> exit loop

    real_pt = repl_mod._read_with_pt
    real_stdin = repl_mod._read_via_stdin
    try:
        repl_mod._read_with_pt = _fake_pt  # type: ignore[assignment]
        repl_mod._read_via_stdin = _fake_stdin  # type: ignore[assignment]
        # Force the not-TTY path regardless of environment.
        real_isatty_in = sys.stdin.isatty
        real_isatty_out = sys.stdout.isatty
        sys.stdin.isatty = lambda: False  # type: ignore[method-assign]
        sys.stdout.isatty = lambda: False  # type: ignore[method-assign]

        try:
            transport = ASGITransport(app=app)

            async def _go() -> int:
                async with app.router.lifespan_context(app):
                    return await repl_mod.run_repl(
                        base_url="http://t",
                        transport=transport,
                    )

            rc = asyncio.run(_go())
        finally:
            sys.stdin.isatty = real_isatty_in  # type: ignore[method-assign]
            sys.stdout.isatty = real_isatty_out  # type: ignore[method-assign]
    finally:
        repl_mod._read_with_pt = real_pt  # type: ignore[assignment]
        repl_mod._read_via_stdin = real_stdin  # type: ignore[assignment]

    assert rc == 0
    assert selected["used_pt"] is False, (
        f"REPL should pick stdin fallback when stdin isn't a TTY, "
        f"got used_pt={selected['used_pt']!r}"
    )
    print("[A3] run_repl falls back to readline when stdin isn't a TTY")


def test_embedded_mode_starts_lifespan_and_exits_cleanly() -> None:
    """`marginalia` (no --server) must mount the FastAPI app in-process.

    Verifies:
      1. _embedded_lifespan() yields an httpx.ASGITransport
      2. Lifespan startup fires (worker_enabled=true → TaskRunner starts)
      3. Health probe through the embedded transport returns 200
      4. Lifespan shutdown completes without raising
    """
    import asyncio
    import httpx

    sandbox = _TEST_ROOT / "embedded_mode"
    sandbox.mkdir(parents=True)
    os.environ["MARGINALIA_HOME"] = str(sandbox)
    os.environ["WORKER_ENABLED"] = "true"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
    os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

    from marginalia.config import get_settings
    get_settings.cache_clear()  # type: ignore[attr-defined]

    from marginalia.db.engine import get_engine
    from marginalia.db.models import Base
    from marginalia.cli import repl as repl_mod

    async def _setup_schema() -> None:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup_schema())

    async def _go() -> dict:
        async with repl_mod._embedded_lifespan() as transport:
            assert isinstance(transport, httpx.ASGITransport)
            async with httpx.AsyncClient(
                base_url=repl_mod.EMBEDDED_BASE_URL, transport=transport,
            ) as c:
                r = await c.get("/health")
                assert r.status_code == 200, r.text
                health_body = r.json()
                # Exercise a /v1 route too — confirms prefix is wired.
                r2 = await c.get("/v1/folders")
                assert r2.status_code == 200, r2.text
                return {"health": health_body, "folders": r2.json()}

    out = asyncio.run(_go())
    assert out["health"] == {"status": "ok", "storage_backend": "local"}
    assert isinstance(out["folders"], dict)
    print("[A4] embedded mode lifespan + ASGI transport round-trip OK")


def test_embedded_main_picks_remote_when_server_arg_given() -> None:
    """`marginalia --server URL` must skip embedded mode (just URL routing).

    We don't actually run the loop — just verify the dispatch logic by
    pulling apart main()'s argv handling: with --server, asyncio.run
    receives run_repl(remote); without, it receives _run_embedded().
    """
    import sys as _sys
    import asyncio

    from marginalia.cli import repl as repl_mod

    captured = {"target": None}

    real_run = asyncio.run

    def _capture(coro):
        captured["target"] = coro.__qualname__
        coro.close()
        return 0

    real_argv = _sys.argv[:]
    real_env_server = os.environ.pop("MARGINALIA_SERVER", None)
    try:
        asyncio.run = _capture  # type: ignore[assignment]

        _sys.argv = ["marginalia"]
        repl_mod.main()
        assert captured["target"] == "_run_embedded", captured

        _sys.argv = ["marginalia", "--server", "http://example:9999"]
        repl_mod.main()
        assert captured["target"] == "run_repl", captured

        os.environ["MARGINALIA_SERVER"] = "http://from-env:8000"
        _sys.argv = ["marginalia"]
        repl_mod.main()
        assert captured["target"] == "run_repl", captured
    finally:
        asyncio.run = real_run  # type: ignore[assignment]
        _sys.argv = real_argv
        os.environ.pop("MARGINALIA_SERVER", None)
        if real_env_server is not None:
            os.environ["MARGINALIA_SERVER"] = real_env_server

    print("[A5] main() picks embedded vs remote correctly")


# ---- main runner -------------------------------------------------------------

def main() -> None:
    test_spinner_no_op_when_not_tty()
    test_spinner_context_manager_runs_without_error()
    test_spinner_animates_when_forced_enabled()
    test_render_table_alignment()
    test_render_table_no_header_separator()
    test_init_project_creates_artifacts()
    test_init_project_idempotent()
    test_init_project_appends_existing_gitignore()
    test_init_render_report()
    test_repl_module_imports_and_main_dispatches_init()
    test_repl_pt_session_buildable()
    test_repl_fallback_when_not_tty()
    test_embedded_mode_starts_lifespan_and_exits_cleanly()
    test_embedded_main_picks_remote_when_server_arg_given()
    print("\nALL CLI UPGRADE E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
