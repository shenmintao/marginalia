"""Non-interactive Marginalia CLI commands.

This module intentionally reuses the existing REPL slash-command handlers for
human-readable output. JSON mode uses the underlying client/service APIs for
the commands that are commonly scripted.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import httpx

from marginalia.agent.types import ChatMode
from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.commands import (
    COMMANDS,
    CliContext,
    _ExitREPL,
    dispatch,
    list_commands,
    parse_reprocess_parts,
)

EMBEDDED_BASE_URL = "http://embedded"
ENV_SERVER = "MARGINALIA_SERVER"
ENV_API_TOKEN = "MARGINALIA_API_TOKEN"

CHAT_COMMANDS = {"ask", "chat"}
ONE_SHOT_COMMANDS = {
    name
    for name in COMMANDS
    if name not in {"quit", "exit", "clear", "new", "cd", "mode"}
}
ONE_SHOT_ALIASES = {"bg": "background"}
ONE_SHOT_NAMES = CHAT_COMMANDS | ONE_SHOT_COMMANDS | set(ONE_SHOT_ALIASES)


class OneShotUsageError(ValueError):
    pass


class OneShotOptions:
    def __init__(self) -> None:
        self.json_output = False
        self.server_url: str | None = None
        self.api_token: str | None = None
        self.mode: ChatMode = "auto"
        self.stdin_prompt = False


def is_one_shot_command(name: str) -> bool:
    return name in ONE_SHOT_NAMES


def _console_error(message: str) -> None:
    print(message, file=sys.stderr)


def _write_json(payload: Any) -> None:
    print(json.dumps(_jsonable(payload), ensure_ascii=False, indent=2))


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return value


def _extract_options(argv: list[str]) -> tuple[list[str], OneShotOptions]:
    opts = OneShotOptions()
    out: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            opts.json_output = True
        elif arg == "--stdin":
            opts.stdin_prompt = True
        elif arg == "--server":
            i += 1
            if i >= len(argv):
                raise OneShotUsageError("--server requires a URL")
            opts.server_url = argv[i]
        elif arg.startswith("--server="):
            opts.server_url = arg.split("=", 1)[1]
        elif arg == "--api-token":
            i += 1
            if i >= len(argv):
                raise OneShotUsageError("--api-token requires a token")
            opts.api_token = argv[i]
        elif arg.startswith("--api-token="):
            opts.api_token = arg.split("=", 1)[1]
        elif arg == "--mode":
            i += 1
            if i >= len(argv):
                raise OneShotUsageError("--mode requires auto, quick, or deep")
            opts.mode = _parse_mode(argv[i])
        elif arg.startswith("--mode="):
            opts.mode = _parse_mode(arg.split("=", 1)[1])
        else:
            out.append(arg)
        i += 1
    return out, opts


def _parse_mode(value: str) -> ChatMode:
    mode = value.strip().lower()
    if mode not in {"auto", "quick", "deep"}:
        raise OneShotUsageError("--mode must be auto, quick, or deep")
    return mode  # type: ignore[return-value]


def _pop_int_option(
    args: list[str],
    *names: str,
    default: int,
) -> tuple[list[str], int]:
    out: list[str] = []
    value = default
    i = 0
    while i < len(args):
        arg = args[i]
        matched_name = next((name for name in names if arg == name), None)
        matched_prefix = next(
            (name for name in names if arg.startswith(f"{name}=")),
            None,
        )
        if matched_name is not None:
            i += 1
            if i >= len(args):
                raise OneShotUsageError(f"{matched_name} requires an integer")
            value = _parse_positive_int(args[i], matched_name)
        elif matched_prefix is not None:
            value = _parse_positive_int(arg.split("=", 1)[1], matched_prefix)
        else:
            out.append(arg)
        i += 1
    return out, value


def _parse_positive_int(raw: str, name: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise OneShotUsageError(f"{name} requires an integer") from exc
    if value <= 0:
        raise OneShotUsageError(f"{name} must be positive")
    return value


def _quote_for_slash(arg: str) -> str:
    if arg == "":
        return '""'
    if re.search(r"\s", arg) or any(ch in arg for ch in "\"'"):
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def _slash_args(args: list[str]) -> str:
    return " ".join(_quote_for_slash(arg) for arg in args)


def _read_prompt(args: list[str], *, stdin_prompt: bool) -> str:
    use_stdin = stdin_prompt or args == ["-"]
    if use_stdin:
        text = sys.stdin.read()
    elif not args and not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        text = " ".join(args)
    text = text.strip()
    if not text:
        raise OneShotUsageError("missing prompt")
    return text


@contextlib.asynccontextmanager
async def _client_context(
    *,
    server_url: str | None,
    api_token: str | None,
    client: MarginaliaClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str | None = None,
) -> AsyncIterator[MarginaliaClient]:
    if client is not None:
        yield client
        return
    if transport is not None:
        owned = MarginaliaClient(
            base_url=base_url or "http://test",
            api_token=api_token,
            transport=transport,
        )
        try:
            yield owned
        finally:
            await owned.aclose()
        return

    target = (server_url or os.environ.get(ENV_SERVER) or "").strip().rstrip("/")
    if not target:
        target = await _discover_remote_url() or ""
    if target:
        owned = MarginaliaClient(base_url=target, api_token=api_token)
        try:
            yield owned
        finally:
            await owned.aclose()
        return

    from asgi_lifespan import LifespanManager

    from marginalia.main import app

    async with LifespanManager(app) as manager:
        owned = MarginaliaClient(
            base_url=EMBEDDED_BASE_URL,
            api_token=api_token,
            transport=httpx.ASGITransport(app=manager.app),
        )
        try:
            yield owned
        finally:
            await owned.aclose()


async def _discover_remote_url() -> str | None:
    try:
        from marginalia.config import get_settings
        from marginalia.server_discovery import discover_server_url

        settings = get_settings()
        return await discover_server_url(settings.marginalia_home)
    except (OSError, RuntimeError, ValueError):
        return None


async def run_async(
    argv: list[str],
    *,
    client: MarginaliaClient | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    base_url: str | None = None,
) -> int:
    if not argv:
        print_help()
        return 0
    try:
        filtered, opts = _extract_options(argv)
        if not filtered:
            raise OneShotUsageError("missing command")
        command_name = ONE_SHOT_ALIASES.get(filtered[0], filtered[0])
        args = filtered[1:]
        api_token = opts.api_token or os.environ.get(ENV_API_TOKEN) or None
        if command_name not in ONE_SHOT_NAMES and command_name not in COMMANDS:
            raise OneShotUsageError(f"unknown command: {filtered[0]}")
        if command_name == "help":
            if opts.json_output:
                _write_json({
                    "ok": True,
                    "commands": [
                        {
                            "name": name.lstrip("/"),
                            "description": doc,
                        }
                        for name, doc in list_commands()
                        if name.lstrip("/") in ONE_SHOT_COMMANDS
                    ],
                    "aliases": sorted(CHAT_COMMANDS | set(ONE_SHOT_ALIASES)),
                })
            else:
                print_help()
            return 0

        async with _client_context(
            server_url=opts.server_url,
            api_token=api_token,
            client=client,
            transport=transport,
            base_url=base_url,
        ) as active_client:
            ctx = CliContext(client=active_client, chat_mode=opts.mode)
            if command_name in CHAT_COMMANDS:
                prompt = _read_prompt(args, stdin_prompt=opts.stdin_prompt)
                payload = await _run_ask(ctx, prompt, mode=opts.mode)
                if opts.json_output:
                    _write_json(payload)
                else:
                    print(payload["answer"])
                return 0
            if opts.json_output:
                payload = await _run_json_command(ctx, command_name, args)
                _write_json(payload)
                return 0
            await _run_text_command(ctx, command_name, args)
            return 0
    except OneShotUsageError as exc:
        if "--json" in argv:
            _write_json({"ok": False, "error": str(exc)})
        else:
            _console_error(str(exc))
        return 2
    except CliHttpError as exc:
        payload = {"ok": False, "status": exc.status, "error": exc.payload}
        if "--json" in argv:
            _write_json(payload)
        else:
            _console_error(f"server error: HTTP {exc.status} {exc.payload}")
        return 2
    except httpx.HTTPStatusError as exc:
        payload = {
            "ok": False,
            "status": exc.response.status_code,
            "error": exc.response.text,
        }
        if "--json" in argv:
            _write_json(payload)
        else:
            _console_error(
                f"server error: HTTP {exc.response.status_code} {exc.response.text}"
            )
        return 2
    except (OSError, ValueError) as exc:
        if "--json" in argv:
            _write_json({"ok": False, "error": str(exc)})
        else:
            _console_error(str(exc))
        return 2


def run(argv: list[str] | None = None) -> int:
    return asyncio.run(run_async(list(sys.argv[1:] if argv is None else argv)))


async def _run_text_command(
    ctx: CliContext,
    command_name: str,
    args: list[str],
) -> None:
    if command_name not in COMMANDS:
        raise OneShotUsageError(f"unknown command: {command_name}")
    try:
        await dispatch(ctx, f"/{command_name} {_slash_args(args)}".rstrip())
    except _ExitREPL:
        return


async def _run_json_command(
    ctx: CliContext,
    command_name: str,
    args: list[str],
) -> dict[str, Any]:
    if command_name == "search":
        args, limit = _pop_int_option(args, "--limit", "-n", default=25)
        query = " ".join(args).strip()
        if not query:
            raise OneShotUsageError("usage: marginalia search <query> [--limit N]")
        payload = await ctx.client.search(query, limit=limit)
        payload["ok"] = True
        payload["query"] = query
        return payload
    if command_name == "info":
        if len(args) != 1:
            raise OneShotUsageError("usage: marginalia info <entry_id>")
        payload = await ctx.client.get_entry_metadata(args[0])
        payload["ok"] = True
        return payload
    if command_name == "discover":
        include_unvetted = "--all" in args
        vet = "--vet" in args
        args = [arg for arg in args if arg not in {"--all", "--vet"}]
        args, top_k = _pop_int_option(args, "--top-k", "-n", default=8)
        if not args:
            raise OneShotUsageError("usage: marginalia discover <entry_id> [--top-k N] [--all] [--vet]")
        if len(args) > 1 and args[1].isdigit():
            top_k = int(args[1])
        payload = await ctx.client.discover(
            args[0],
            top_k=top_k,
            include_unvetted=include_unvetted,
            vet=vet,
        )
        payload["ok"] = True
        return payload
    if command_name == "reprocess":
        try:
            body, label = parse_reprocess_parts(args)
        except ValueError as exc:
            raise OneShotUsageError(str(exc)) from exc
        payload = await ctx.client.reprocess_bulk(body)
        payload["ok"] = True
        payload["scope"] = label
        return payload
    if command_name in {"background", "bg"}:
        args, limit = _pop_int_option(args, "--limit", "-n", default=30)
        if args:
            raise OneShotUsageError("usage: marginalia background [--limit N]")
        payload = await ctx.client.list_active_tasks(limit=limit)
        payload["ok"] = True
        return payload
    if command_name == "ls":
        if len(args) > 1:
            raise OneShotUsageError("usage: marginalia ls [parent_id]")
        payload = await ctx.client.list_folder(parent_id=(args[0] if args else None))
        payload["ok"] = True
        return payload
    if command_name == "upload":
        if len(args) < 2:
            raise OneShotUsageError("usage: marginalia upload <local_path> <remote_path>")
        payload = await ctx.client.upload_file(local_path=args[0], remote_path=args[1])
        payload["ok"] = True
        return payload
    if command_name == "download":
        return await _download_json(ctx, args)
    if command_name == "export":
        return await _export_json(ctx, args)
    if command_name == "tend":
        if len(args) > 1:
            raise OneShotUsageError("usage: marginalia tend [run_id]")
        payload = await (
            ctx.client.tend_status(args[0]) if args else ctx.client.tend_start()
        )
        payload["ok"] = True
        return payload
    if command_name == "check":
        report = await _scan_report()
        payload = _scan_report_payload(report)
        payload["ok"] = True
        return payload
    if command_name == "ingest":
        return await _ingest_json(ctx, args)
    return await _generic_json_text(ctx, command_name, args)


async def _download_json(ctx: CliContext, args: list[str]) -> dict[str, Any]:
    force_folder = "--folder" in args
    args = [arg for arg in args if arg != "--folder"]
    if not args:
        raise OneShotUsageError("usage: marginalia download <entry_id|folder_id> [dest] [--folder]")
    target_id = args[0]
    dest = Path(args[1]) if len(args) > 1 else None
    if not force_folder:
        try:
            meta = await ctx.client.get_entry_metadata(target_id)
        except CliHttpError as exc:
            if exc.status != 404:
                raise
            meta = None
        if meta is not None:
            out = await ctx.client.download_entry(
                target_id,
                dest=dest or Path.cwd() / meta["display_name"],
            )
            out["ok"] = True
            out["kind"] = "file"
            return out
    out = await ctx.client.download_folder(
        target_id,
        dest=dest or Path.cwd() / f"{target_id[:8]}.zip",
    )
    out["ok"] = True
    out["kind"] = "folder"
    return out


async def _export_json(ctx: CliContext, args: list[str]) -> dict[str, Any]:
    if len(args) > 2:
        raise OneShotUsageError("usage: marginalia export [conversation_id] [dest.md|dest.zip]")
    conversation_id = args[0] if args else None
    if conversation_id is None:
        latest = await ctx.client.latest_conversation()
        if latest is None:
            raise OneShotUsageError("no ended conversation found")
        conversation_id = latest["conversation_id"]
    dest = Path(args[1]) if len(args) > 1 else Path.cwd() / f"conversation-{conversation_id[:8]}.zip"
    out = (
        await ctx.client.export_conversation_markdown(conversation_id, dest=dest)
        if dest.suffix.lower() == ".md"
        else await ctx.client.export_conversation(conversation_id, dest=dest)
    )
    out["ok"] = True
    return out


async def _ingest_json(ctx: CliContext, args: list[str]) -> dict[str, Any]:
    if "--all" not in args:
        return await _generic_json_text(ctx, "ingest", args)
    report = await _scan_report()
    payload = _scan_report_payload(report)
    if report.total_changes == 0:
        return {"ok": True, "applied": False, "report": payload}
    if "--yes" not in args:
        return {
            "ok": True,
            "applied": False,
            "requires_confirmation": True,
            "report": payload,
        }

    from marginalia.services.sync import apply_all

    out = await apply_all(report)
    failures = [_jsonable(failure) for failure in out.get("failures") or []]
    return {
        "ok": not failures,
        "applied": True,
        "ingested": out["ingested"],
        "modified": out["modified"],
        "moved": out["moved"],
        "forgotten": out["forgotten"],
        "failures": failures,
        "report": payload,
    }


async def _generic_json_text(
    ctx: CliContext,
    command_name: str,
    args: list[str],
) -> dict[str, Any]:
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        await _run_text_command(ctx, command_name, args)
    return {
        "ok": True,
        "command": command_name,
        "output": buf.getvalue(),
    }


async def _scan_report():
    from marginalia.config import get_settings
    from marginalia.services.scan import scan_vault
    from marginalia.storage import MirrorStorage, get_storage

    storage = get_storage()
    if not isinstance(storage, MirrorStorage):
        raise OneShotUsageError("check/ingest require STORAGE_BACKEND=mirror")
    settings = get_settings()
    return await scan_vault(Path(settings.mirror_vault_root))


def _scan_report_payload(report) -> dict[str, Any]:
    def _entry_payload(entry) -> dict[str, Any]:
        return {
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "file_id": entry.file_id,
            "folder_id": entry.folder_id,
        }

    return {
        "vault_root": str(report.vault_root),
        "in_sync": report.in_sync_count,
        "new": [
            {
                "path": str(path),
                "relative_path": path.relative_to(report.vault_root).as_posix(),
                "size_bytes": path.stat().st_size,
            }
            for path in report.new
        ],
        "modified": [
            {
                "entry": _entry_payload(entry),
                "path": str(path),
                "relative_path": path.relative_to(report.vault_root).as_posix(),
            }
            for entry, path in report.modified
        ],
        "missing": [_entry_payload(entry) for entry in report.missing],
        "moved": [
            {
                "entry": _entry_payload(entry),
                "path": str(path),
                "relative_path": path.relative_to(report.vault_root).as_posix(),
            }
            for entry, path in report.moved
        ],
        "total_changes": report.total_changes,
    }


async def _run_ask(
    ctx: CliContext,
    prompt: str,
    *,
    mode: ChatMode,
) -> dict[str, Any]:
    session = await ctx.client.create_session(initiating_user_message=prompt)
    session_id = session["session_id"]
    answer_parts: list[str] = []
    conversation_id: str | None = None
    plan: str | None = None
    done: dict[str, Any] | None = None
    tool_events = 0
    try:
        async for event in ctx.client.stream_chat(session_id, prompt, mode=mode):
            if event.event_type == "conversation":
                conversation_id = event.data
            elif event.event_type == "plan":
                plan = event.data
            elif event.event_type == "answer":
                answer_parts.append(event.data)
            elif event.event_type in {"tool_call", "tool_result"}:
                tool_events += 1
            elif event.event_type == "done":
                try:
                    done = json.loads(event.data)
                except ValueError:
                    done = {"raw": event.data}
            elif event.event_type == "error":
                raise OneShotUsageError(event.data)
    finally:
        with contextlib.suppress(Exception):
            await ctx.client.close_session(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "answer": "".join(answer_parts),
        "plan": plan,
        "done": done,
        "tool_events": tool_events,
    }


def print_help() -> None:
    print("Usage: marginalia <command> [args] [--json]")
    print()
    print("One-shot commands:")
    print("  ask <prompt>                 ask one question and print the answer")
    print("  chat <prompt>                alias for ask")
    for name, doc in list_commands():
        raw = name.lstrip("/")
        if raw in ONE_SHOT_COMMANDS:
            print(f"  {raw:<28} {doc}")
    print()
    print("Global options: --json --server URL --api-token TOKEN --mode auto|quick|deep")
