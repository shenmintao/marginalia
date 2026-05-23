"""Interactive REPL loop for the Marginalia CLI.

Backed by prompt_toolkit when stdin is a TTY:
  - Tab-completion on `/<command>` (slash completer)
  - Persisted history (`~/.marginalia_history`)
  - Smart Ctrl-C: cancels current line if any input typed, exits at empty prompt
  - Multi-line via Esc+Enter or Alt+Enter

When stdin is not a TTY (pipes / tests), falls back to the original
`sys.stdin.readline` loop so e2e tests and shell scripts keep working.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import httpx

from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.commands import (
    CliContext,
    _ExitREPL,
    dispatch,
    list_commands,
)

PROMPT = "marginalia> "
HISTORY_PATH = Path.home() / ".marginalia_history"


def _print_banner(ctx: CliContext) -> None:
    print()
    print("Marginalia CLI")
    print(f"  server: {ctx.client.base_url}")
    print(f"  cwd:    {ctx.cwd_remote}")
    print(f"  on_conflict: {ctx.on_conflict}")
    print()
    print("type /help for commands, or just type a question.")
    print("  Tab        — complete /<command>")
    print("  Ctrl-C     — cancel current line (or quit when empty)")
    print("  Ctrl-D     — exit")
    print("  Alt+Enter  — newline (multi-line input)")
    print()


# ---- prompt_toolkit-based reader (interactive TTY) ------------------------

def _make_slash_completer():
    """Build a Completer that suggests `/<command>` names from the registry.

    Factored out so tests can exercise completion without spinning up a
    full PromptSession (which on Windows requires a real console buffer)."""
    from prompt_toolkit.completion import Completer, Completion

    completion_strs = [name for name, _ in list_commands()]

    class SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/"):
                return
            for c in completion_strs:
                if c.startswith(text):
                    yield Completion(c, start_position=-len(text))

    return SlashCompleter()


def _build_pt_session():
    """Lazy-build a prompt_toolkit PromptSession with completion + history."""
    from prompt_toolkit import PromptSession
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
        message=PROMPT,
        completer=_make_slash_completer(),
        history=FileHistory(str(history_path)) if history_path else None,
        key_bindings=bindings,
        complete_while_typing=False,
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


# ---- main loop ------------------------------------------------------------

async def run_repl(
    *,
    base_url: str = "http://127.0.0.1:8000",
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    client = MarginaliaClient(base_url=base_url, transport=transport)
    ctx = CliContext(client=client)

    # Decide reader strategy: if stdin is a TTY, use prompt_toolkit; else
    # fall back to plain readline so pipes / tests work.
    use_pt = sys.stdin.isatty() and sys.stdout.isatty()
    pt_session = _build_pt_session() if use_pt else None

    try:
        try:
            await client.health()
        except Exception as exc:  # noqa: BLE001
            print(f"cannot reach server at {base_url}: {exc}")
            return 2

        _print_banner(ctx)

        while True:
            if pt_session is not None:
                line = await _read_with_pt(pt_session)
                if line is None:
                    print()
                    break
            else:
                sys.stdout.write(PROMPT)
                sys.stdout.flush()
                line = await _read_via_stdin()
                if line is None:
                    print()
                    break

            try:
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
        return 0
    finally:
        await client.aclose()


def main() -> int:
    import argparse

    argv = sys.argv[1:]
    # Detect subcommand BEFORE argparse to avoid clashing with REPL's --server.
    if argv and argv[0] == "init":
        from marginalia.cli.init_cmd import cmd_init_main
        return cmd_init_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="marginalia",
        description=(
            "Marginalia CLI. Run with no args for the REPL, "
            "or `marginalia init` to bootstrap a project."
        ),
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:8000",
        help="Marginalia server base URL (default %(default)s)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_repl(base_url=args.server))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
