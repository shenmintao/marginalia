"""Interactive REPL loop for the Marginalia CLI.

Backed by prompt_toolkit when stdin is a TTY:
  - Tab-completion on `/<command>` (slash completer)
  - Persisted history (`~/.marginalia_history`)
  - Smart Ctrl-C: cancels current line if any input typed, exits at empty prompt
  - Multi-line via Esc+Enter or Alt+Enter

When stdin is not a TTY (pipes / tests), falls back to the original
`sys.stdin.readline` loop so e2e tests and shell scripts keep working.

## Embedded vs remote mode

Default is **embedded**: the FastAPI app is mounted in-process and the
TaskRunner is started inside the CLI's own asyncio loop. No HTTP socket
is opened — `httpx.ASGITransport` invokes the ASGI app directly.

When `--server <URL>` is passed (or `MARGINALIA_SERVER` env is set), the
CLI runs in **remote** mode: a normal HTTP client targeting the URL.
Use this when running the server on a different machine or when sharing
one knowledge base across multiple CLIs.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx

from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.commands import (
    CliContext,
    _ExitREPL,
    dispatch,
    list_commands,
)
from marginalia.cli.render import (
    BOLD,
    DIM,
    DIM_GREY,
    RESET,
    print_banner,
)

PROMPT = f"{DIM_GREY}❯{RESET} "
HISTORY_PATH = Path.home() / ".marginalia_history"
EMBEDDED_BASE_URL = "http://embedded"
EMBEDDED_MARKER = "embedded"
ENV_SERVER = "MARGINALIA_SERVER"


def _build_prompt(ctx: CliContext, *, pending: int = 0) -> str:
    """Compose the REPL prompt — a green `❯` and nothing else.

    Backend / cwd / busy-count used to be inlined here; they cluttered the
    line and froze the screen rhythm. Use `/busy` and `/cd` to query that
    state on demand instead.
    """
    return PROMPT


def _build_toolbar(ctx: CliContext, *, pending: int = 0) -> str:
    """Single-line status bar shown at the bottom of the prompt.

    Mirrors claude-code's PromptInputFooter spirit: cwd on the left,
    a couple of hints on the right. Kept dim so it doesn't pull focus
    from the answer stream above it.
    """
    cwd = ctx.cwd_remote or "/"
    busy = f"  {pending} busy" if pending > 0 else ""
    hint = "/help  Alt+Enter newline  Ctrl+D exit"
    return f"{DIM_GREY}{cwd}{busy}    {hint}{RESET}"


def _print_banner(ctx: CliContext, mode: str) -> None:
    try:
        from marginalia import __version__ as _ver
    except Exception:
        _ver = ""
    title = f"Marginalia{(' v' + _ver) if _ver else ''}"

    if mode == EMBEDDED_MARKER:
        backend = "embedded"
    else:
        backend = ctx.client.base_url
    storage = ctx.storage_backend or "?"

    lines = [
        f"{BOLD}Welcome to Marginalia{RESET}  {DIM}- a personal library{RESET}",
        f"{DIM_GREY}backend{RESET} {backend}    {DIM_GREY}storage{RESET} {storage}",
        f"{DIM_GREY}cwd{RESET}     {ctx.cwd_remote}",
        f"{DIM}/help for commands. Or just type a question.{RESET}",
    ]
    print()
    print_banner(title, lines)
    print()


# ---- prompt_toolkit-based reader (interactive TTY) ------------------------

def _make_slash_completer(ctx: CliContext | None = None):
    """Build a Completer that suggests `/<command>` names plus argument
    completions populated from prior commands.

    What it completes:
      * `/<command>` at the start of the line  → command names
      * `/info | /discover | /download <prefix>`  → entry-id prefixes the
        user has already seen (via /search, /info, /discover, /ls)
      * `/cd <path>`                           → folder paths from /tree
      * `/upload <local> <remote>`             → folder paths for the
                                                 second positional arg
      * fall-through                           → no suggestions

    No background fetches — completion only surfaces what the user has
    pulled into ctx via earlier commands. Same intuition as shell history.
    """
    from prompt_toolkit.completion import Completer, Completion

    completion_strs = [name for name, _ in list_commands()]
    ENTRY_ID_CMDS = {"/info", "/discover", "/download"}
    PATH_CMDS = {"/cd"}

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            # Command-name completion: still on the first token.
            if " " not in text:
                for c in completion_strs:
                    if c.startswith(text):
                        yield Completion(c, start_position=-len(text))
                return
            if ctx is None:
                return
            cmd, _, rest = text.partition(" ")
            word = document.get_word_before_cursor(WORD=True)
            if cmd in ENTRY_ID_CMDS:
                for eid, name in ctx.seen_entry_ids.items():
                    if eid.startswith(word):
                        yield Completion(
                            eid, start_position=-len(word),
                            display_meta=name[:40],
                        )
            elif cmd in PATH_CMDS:
                for p in ctx.seen_folder_paths:
                    if p.startswith(word):
                        yield Completion(p, start_position=-len(word))
            elif cmd == "/upload":
                # Second positional arg only — first is a local path.
                tokens = rest.split()
                wants_remote = (
                    len(tokens) >= 2
                    or (len(tokens) == 1 and rest.endswith(" "))
                )
                if wants_remote:
                    for p in ctx.seen_folder_paths:
                        if p.startswith(word):
                            yield Completion(p, start_position=-len(word))

    return SlashCompleter()


def _build_pt_session(prompt_fn, ctx: CliContext | None = None, *, toolbar_fn=None):
    """Lazy-build a prompt_toolkit PromptSession with completion + history.

    `prompt_fn` is called once per prompt cycle to render the message
    string — this is how `marginalia[mirror /research • 12 busy]>` stays
    in sync without polling on every keystroke. `ctx` is passed to the
    completer so it can suggest entry-ids and folder paths the user has
    already encountered. `toolbar_fn` (optional) renders a single-line
    bottom toolbar — kept callable so it picks up live state on each redraw.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings

    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _(event):
        """Alt+Enter inserts a newline (so users can submit multi-line text)."""
        event.app.current_buffer.newline()

    history_path = HISTORY_PATH
    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.touch(exist_ok=True)
    except Exception:
        history_path = None  # fall back to in-memory

    return PromptSession(
        message=lambda: ANSI(prompt_fn()),
        bottom_toolbar=(lambda: ANSI(toolbar_fn())) if toolbar_fn else None,
        completer=_make_slash_completer(ctx),
        history=FileHistory(str(history_path)) if history_path else None,
        key_bindings=bindings,
        complete_while_typing=True,
    )


async def _read_with_pt(session) -> Optional[str]:
    """Returns None on EOF / Ctrl-C-at-empty, str otherwise."""
    from prompt_toolkit.patch_stdout import patch_stdout

    try:
        with patch_stdout():
            line = await session.prompt_async()
    except (EOFError, KeyboardInterrupt):
        return None
    return line


# ---- fallback reader (non-TTY: pipes, ASGI tests) ------------------------

async def _read_via_stdin() -> Optional[str]:
    loop = asyncio.get_running_loop()
    try:
        line = await loop.run_in_executor(None, sys.stdin.readline)
    except (EOFError, KeyboardInterrupt):
        return None
    if not line:
        return None
    return line.rstrip("\n")


# ---- transport selection -------------------------------------------------

@contextlib.asynccontextmanager
async def _embedded_lifespan() -> AsyncIterator[httpx.AsyncBaseTransport]:
    """Yield an in-process ASGI transport with FastAPI lifespan running.

    LifespanManager fires startup (TaskRunner.start) on enter and
    shutdown (TaskRunner.stop, dispose_engine) on exit, the same way
    uvicorn would. Until shutdown completes, in-flight ingest tasks keep
    progressing.
    """
    from asgi_lifespan import LifespanManager

    from marginalia.main import app

    async with LifespanManager(app) as manager:
        yield httpx.ASGITransport(app=manager.app)


async def _flush_pending_tasks_prompt(
    client: MarginaliaClient, *, mode: str
) -> None:
    """Embedded mode only: if tasks are still on the queue, ask the user
    whether to wait for them or quit now.

    Wait path = poll running-count every 1s until it hits zero.
    Quit-now path = return; lifespan shutdown will tear down TaskRunner;
    `recover_stuck_tasks` resumes them on next launch.
    """
    if mode != EMBEDDED_MARKER:
        return
    try:
        counts = await client.running_task_count()
    except Exception:
        return
    total = counts.get("running", 0) + counts.get("pending", 0)
    if total <= 0:
        return

    print(
        f"\n{total} background task(s) still queued "
        f"({counts.get('running', 0)} running, {counts.get('pending', 0)} pending)."
    )
    print("[w]ait for them, or [q]uit now (next launch will resume)? ", end="")
    sys.stdout.flush()
    try:
        choice = (await _read_via_stdin()) or ""
    except (EOFError, KeyboardInterrupt):
        choice = ""
    if choice.strip().lower() not in ("w", "wait", ""):
        return  # quit now

    print("waiting for tasks to finish (Ctrl-C to abandon)...")
    try:
        last = total
        while True:
            await asyncio.sleep(1.0)
            counts = await client.running_task_count()
            now = counts.get("running", 0) + counts.get("pending", 0)
            if now <= 0:
                print("all tasks done.")
                return
            if now != last:
                print(
                    f"  {now} task(s) remaining "
                    f"({counts.get('running', 0)} running, "
                    f"{counts.get('pending', 0)} pending)"
                )
                last = now
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("(abandoning wait — tasks will resume on next launch)")


# ---- main loop ------------------------------------------------------------

async def _refresh_pending_count(client: MarginaliaClient) -> int:
    """Sum of running + pending tasks. Used to drive `N busy` in the prompt.

    Called once per prompt cycle (between iterations of the read loop), not
    per keystroke — so a slow DB doesn't block typing. Failures degrade to
    zero rather than break the prompt.
    """
    try:
        counts = await client.running_task_count()
    except Exception:
        return 0
    return counts.get("running", 0) + counts.get("pending", 0)


async def run_repl(
    *,
    base_url: str = "http://127.0.0.1:8000",
    transport: httpx.AsyncBaseTransport | None = None,
    mode: str = "remote",
) -> int:
    """Run the REPL.

    Parameters
    ----------
    base_url
        For remote mode: server URL. For embedded mode: a sentinel
        ``http://embedded`` is used (httpx requires a base URL even with
        a custom transport).
    transport
        Optional httpx transport. Set when embedded mode prepares an
        ASGITransport in advance, or when tests inject a fake.
    mode
        ``"embedded"`` or ``"remote"`` — affects only the banner.
    """
    client = MarginaliaClient(base_url=base_url, transport=transport)
    ctx = CliContext(client=client)
    pending_count = 0

    use_pt = sys.stdin.isatty() and sys.stdout.isatty()
    pt_session = (
        _build_pt_session(
            lambda: _build_prompt(ctx, pending=pending_count),
            ctx,
            toolbar_fn=lambda: _build_toolbar(ctx, pending=pending_count),
        )
        if use_pt else None
    )

    try:
        try:
            health = await client.health()
        except Exception as exc:  # noqa: BLE001
            target = "embedded server" if mode == EMBEDDED_MARKER else base_url
            print(f"cannot reach {target}: {exc}")
            return 2
        ctx.storage_backend = health.get("storage_backend", "?")

        _print_banner(ctx, mode)

        while True:
            pending_count = await _refresh_pending_count(client)
            if pt_session is not None:
                line = await _read_with_pt(pt_session)
                if line is None:
                    print()
                    break
            else:
                sys.stdout.write(_build_prompt(ctx, pending=pending_count))
                sys.stdout.flush()
                line = await _read_via_stdin()
                if line is None:
                    print()
                    break

            try:
                # Buffer the user's input from subsequent output (session
                # creation, spinner, answer) — gives every turn a clean
                # opening line.
                print()
                await dispatch(ctx, line)
            except _ExitREPL:
                break
            except KeyboardInterrupt:
                print("\n(interrupted)")
                continue
            except CliHttpError as e:
                print(f"server error: HTTP {e.status} {e.payload}")
            except Exception as e:  # noqa: BLE001
                print(f"client error: {e!r}")

        if ctx.session_id is not None:
            try:
                await client.close_session(ctx.session_id)
            except Exception:
                pass
        await _flush_pending_tasks_prompt(client, mode=mode)
        return 0
    finally:
        await client.aclose()


async def _run_embedded() -> int:
    async with _embedded_lifespan() as transport:
        return await run_repl(
            base_url=EMBEDDED_BASE_URL,
            transport=transport,
            mode=EMBEDDED_MARKER,
        )


def main() -> int:
    import argparse

    argv = sys.argv[1:]
    # Detect subcommand BEFORE argparse to avoid clashing with REPL's --server.
    if argv and argv[0] == "init":
        from marginalia.cli.init_cmd import cmd_init_main
        return cmd_init_main(argv[1:])
    if argv and argv[0] == "storage":
        from marginalia.cli.storage_cmd import cmd_storage_main
        return cmd_storage_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="marginalia",
        description=(
            "Marginalia CLI. Run with no args for the embedded REPL, "
            "`--server URL` (or MARGINALIA_SERVER env) for remote mode, "
            "or `marginalia init` to bootstrap a project."
        ),
    )
    parser.add_argument(
        "--server", default=None,
        help="Server URL for remote mode. If omitted, runs in-process "
             "(reads MARGINALIA_SERVER env as fallback).",
    )
    args = parser.parse_args(argv)
    server_url = args.server or os.environ.get(ENV_SERVER) or None

    try:
        if server_url:
            return asyncio.run(run_repl(base_url=server_url, mode="remote"))
        return asyncio.run(_run_embedded())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
