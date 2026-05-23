"""Slash command registry for the Marginalia CLI.

Style: Claude Code-like. The user types `/<name> <args>` to invoke a
command; anything else is forwarded to the agent as chat.

Adding a command: write `async def cmd_xxx(ctx, args_str)` and register it
via the @command decorator. Help text is its docstring's first line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, MutableMapping

from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.render import Spinner, print_markdown


@dataclass
class CliContext:
    """Mutable per-REPL state."""
    client: MarginaliaClient
    session_id: str | None = None
    on_conflict: str = "rename"
    cwd_remote: str = "/"  # for resolving relative remote paths
    history: list[dict] = field(default_factory=list)


CommandHandler = Callable[[CliContext, str], Awaitable[None]]
COMMANDS: MutableMapping[str, CommandHandler] = {}
DOCS: MutableMapping[str, str] = {}


def command(name: str) -> Callable[[CommandHandler], CommandHandler]:
    def deco(fn: CommandHandler) -> CommandHandler:
        if name in COMMANDS:
            raise RuntimeError(f"command {name!r} already registered")
        COMMANDS[name] = fn
        DOCS[name] = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        return fn
    return deco


def list_commands() -> list[tuple[str, str]]:
    return sorted(((f"/{n}", DOCS.get(n, "")) for n in COMMANDS))


# ---- helpers --------------------------------------------------------------

def _resolve_remote(ctx: CliContext, raw: str) -> str:
    """Resolve a remote path relative to ctx.cwd_remote.

    Absolute paths (starting with `/`) bypass cwd. Relative paths are
    appended to cwd. Trailing slash is preserved (it carries semantic
    meaning in the upload API)."""
    if raw.startswith("/"):
        return raw
    base = ctx.cwd_remote.rstrip("/")
    if not base:
        base = ""
    trailing = "/" if raw.endswith("/") else ""
    return f"{base}/{raw.rstrip('/')}{trailing}"


def _split_first(arg_str: str) -> tuple[str, str]:
    arg_str = arg_str.strip()
    if not arg_str:
        return "", ""
    parts = arg_str.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


# ---- command implementations ----------------------------------------------

@command("help")
async def cmd_help(ctx: CliContext, args: str) -> None:
    """List available slash commands."""
    print("\nAvailable slash commands:")
    for name, doc in list_commands():
        print(f"  {name:<20} {doc}")
    print("\nAnything not starting with '/' is treated as chat with the agent.")
    print(f"current cwd: {ctx.cwd_remote!r}\n")


@command("quit")
async def cmd_quit(ctx: CliContext, args: str) -> None:
    """Exit the CLI."""
    raise _ExitREPL()


@command("exit")
async def cmd_exit(ctx: CliContext, args: str) -> None:
    """Exit the CLI (alias of /quit)."""
    raise _ExitREPL()


@command("clear")
async def cmd_clear(ctx: CliContext, args: str) -> None:
    """End the current chat session and start a fresh one."""
    if ctx.session_id is not None:
        try:
            await ctx.client.close_session(ctx.session_id)
        except Exception as e:  # noqa: BLE001
            print(f"  (close failed: {e})")
        ctx.session_id = None
        ctx.history.clear()
    print("session cleared. next chat will open a new session.")


@command("new")
async def cmd_new(ctx: CliContext, args: str) -> None:
    """Open a new chat session explicitly (chat does this lazily)."""
    if ctx.session_id is not None:
        await cmd_clear(ctx, "")
    out = await ctx.client.create_session(initiating_user_message=args or None)
    ctx.session_id = out["session_id"]
    print(f"session: {ctx.session_id} (started_at: {out.get('started_at')})")


@command("cd")
async def cmd_cd(ctx: CliContext, args: str) -> None:
    """Change the working remote path (used to resolve relative paths)."""
    target = (args or "/").strip()
    if not target.startswith("/"):
        target = _resolve_remote(ctx, target)
    if not target.endswith("/"):
        target = target + "/"
    ctx.cwd_remote = target
    print(f"cwd: {ctx.cwd_remote}")


@command("ls")
async def cmd_ls(ctx: CliContext, args: str) -> None:
    """List folders + entries at root or under a folder id."""
    parent_id = args.strip() or None
    out = await ctx.client.list_folders(parent_id=parent_id)
    folders = out.get("folders") or []
    if not folders:
        print("(no folders)")
        return
    print(f"\n{'NAME':<30} {'ID':<38}")
    print("-" * 70)
    for f in folders:
        print(f"{f['name']:<30} {f['id']:<38}")
    print()


@command("tree")
async def cmd_tree(ctx: CliContext, args: str) -> None:
    """Show the folder tree (depth-limited)."""
    max_depth = 4
    if args.strip().isdigit():
        max_depth = int(args.strip())

    async def _walk(parent_id: str | None, depth: int, prefix: str) -> None:
        if depth > max_depth:
            return
        out = await ctx.client.list_folders(parent_id=parent_id)
        folders = out.get("folders") or []
        for i, f in enumerate(folders):
            last = i == len(folders) - 1
            connector = "└── " if last else "├── "
            print(f"{prefix}{connector}{f['name']}  ({f['id'][:8]}…)")
            await _walk(f["id"], depth + 1, prefix + ("    " if last else "│   "))

    print()
    await _walk(None, 0, "")
    print()


@command("upload")
async def cmd_upload(ctx: CliContext, args: str) -> None:
    """/upload <local_path> <remote_path>  — upload a single file."""
    local, remote = _split_first(args)
    if not local or not remote:
        print("usage: /upload <local_path> <remote_path>")
        print("  remote_path: trailing '/' = folder; with extension = filename;")
        print("  ambiguous (no ext, no '/') needs --name <display_name>")
        return

    display_name: str | None = None
    if "--name" in remote:
        # crude parse: split off --name <value>
        bits = remote.split()
        try:
            i = bits.index("--name")
            display_name = bits[i + 1]
            remote = " ".join(bits[:i] + bits[i + 2:])
        except (ValueError, IndexError):
            print("bad --name flag")
            return

    full_remote = _resolve_remote(ctx, remote)
    try:
        out = await ctx.client.upload_file(
            local_path=local,
            remote_path=full_remote,
            display_name=display_name,
            on_conflict=ctx.on_conflict,
        )
    except CliHttpError as e:
        print(f"upload failed: HTTP {e.status} {e.payload}")
        return
    print(
        f"uploaded {Path(local).name} -> {full_remote}\n"
        f"  entry={out['entry_id']}  display={out['display_name']}"
        + ("  (deduped)" if out.get("deduped") else "")
        + ("  (auto-renamed)" if out.get("auto_renamed") else "")
        + ("  (skipped)" if out.get("skipped") else "")
    )


@command("on-conflict")
async def cmd_on_conflict(ctx: CliContext, args: str) -> None:
    """Set name-conflict policy (rename/error/skip)."""
    arg = args.strip().lower()
    if arg not in ("rename", "error", "skip"):
        print(f"current: {ctx.on_conflict}. usage: /on-conflict rename|error|skip")
        return
    ctx.on_conflict = arg
    print(f"on_conflict = {arg}")


@command("search")
async def cmd_search(ctx: CliContext, args: str) -> None:
    """/search <query>  — find files by name or content summary."""
    q = args.strip()
    if not q:
        print("usage: /search <query>")
        return
    out = await ctx.client.search(q, limit=25)
    entries = out.get("entries") or []
    if not entries:
        print(f"no matches for {q!r}")
        return
    print(f"\n{len(entries)} result(s):\n")
    print(f"  {'NAME':<36} {'PATH':<32} {'ENTRY':<12}")
    print("  " + "-" * 80)
    for e in entries:
        eid_short = e["entry_id"][:8] + "…"
        name = e["display_name"]
        if len(name) > 35:
            name = name[:32] + "…"
        path = e["folder_path"]
        if len(path) > 31:
            path = "…" + path[-30:]
        print(f"  {name:<36} {path:<32} {eid_short:<12}")
    print()


@command("info")
async def cmd_info(ctx: CliContext, args: str) -> None:
    """/info <entry_id>  — show user-visible metadata for an entry."""
    eid = args.strip()
    if not eid:
        print("usage: /info <entry_id>")
        return
    try:
        meta = await ctx.client.get_entry_metadata(eid)
    except CliHttpError as e:
        print(f"info failed: HTTP {e.status} {e.payload}")
        return
    summary = meta.get("summary") or "(not yet indexed)"
    size = meta.get("size_bytes") or 0
    print(f"""
  entry:    {meta['entry_id']}
  name:     {meta['display_name']}
  folder:   {meta['folder_path']}
  size:     {size:,} bytes
  type:     {meta.get('mime_type') or '?'}
  ext:      {meta.get('original_ext') or '?'}
  sha256:   {meta.get('sha256', '')[:16]}…
  state:    {meta['lifecycle']}  (ingest={meta.get('ingest_status')})
  created:  {meta.get('created_at') or '?'}
  updated:  {meta.get('updated_at') or '?'}

  summary:
  {summary}
""")


@command("export")
async def cmd_export(ctx: CliContext, args: str) -> None:
    """/export [<conv_id>] [<dest.zip>]  — export a conversation report + cited files.

    Resolution order when conv_id is omitted:
      1. ctx.history's last conversation (this CLI's most recent chat)
      2. server's GET /conversations/latest (most recent ended conversation)
      3. error message if neither exists
    """
    parts = args.strip().split()
    conv_id: str | None = None
    dest_str: str | None = None
    if parts:
        conv_id = parts[0]
        if len(parts) > 1:
            dest_str = parts[1]
    if conv_id is None:
        if ctx.history:
            conv_id = ctx.history[-1]["conversation_id"]
        else:
            try:
                latest = await ctx.client.latest_conversation()
            except CliHttpError as e:
                print(f"could not look up latest conversation: HTTP {e.status} {e.payload}")
                return
            if latest is None:
                print("no ended conversation found on the server.")
                print("usage: /export <conv_id> [<dest.zip>]")
                return
            conv_id = latest["conversation_id"]
            preview = latest.get("user_message_preview") or ""
            print(f"(no id given; using server's most recent conversation: "
                  f"{conv_id[:8]}… \"{preview}\")")

    dest = Path(dest_str) if dest_str else (
        Path.cwd() / f"conversation-{conv_id[:8]}.zip"
    )
    try:
        out = await ctx.client.export_conversation(conv_id, dest=dest)
    except CliHttpError as e:
        print(f"export failed: HTTP {e.status} {e.payload}")
        return
    print(
        f"exported {out['bytes_written']:,} bytes -> {out['saved_to']}\n"
        f"  citations: {out['citation_count']} "
        f"(missing: {out['missing_count']})"
    )


@command("download")
async def cmd_download(ctx: CliContext, args: str) -> None:
    """/download <entry_id|folder_id> [<local_path>]  — file → bytes; folder → zip."""
    parts = args.strip().split()
    if not parts:
        print("usage: /download <entry_id|folder_id> [<local_path>] [--folder]")
        return

    force_folder = False
    if "--folder" in parts:
        force_folder = True
        parts = [p for p in parts if p != "--folder"]
    if not parts:
        print("missing id")
        return
    target_id = parts[0]
    dest_str = parts[1] if len(parts) > 1 else None

    if not force_folder:
        try:
            meta = await ctx.client.get_entry_metadata(target_id)
        except CliHttpError as e:
            meta = None
            if e.status != 404:
                print(f"download failed: HTTP {e.status} {e.payload}")
                return
        if meta is not None:
            dest = Path(dest_str) if dest_str else Path.cwd() / meta["display_name"]
            try:
                out = await ctx.client.download_entry(target_id, dest=dest)
            except CliHttpError as e:
                print(f"download failed: HTTP {e.status} {e.payload}")
                return
            print(f"saved {out['bytes_written']:,} bytes -> {out['saved_to']}")
            return

    dest = Path(dest_str) if dest_str else Path.cwd() / f"{target_id[:8]}.zip"
    if dest.is_dir():
        dest = dest / f"{target_id[:8]}.zip"
    try:
        out = await ctx.client.download_folder(target_id, dest=dest)
    except CliHttpError as e:
        print(f"folder download failed: HTTP {e.status} {e.payload}")
        return
    print(
        f"saved zip ({out['member_count']} files, "
        f"{out['bytes_written']:,} bytes) -> {out['saved_to']}"
    )
    """/download <entry_id|folder_id> [<local_path>]  — file → bytes; folder → zip.

    The id is tried as an entry first; on 404 we fall back to folder
    download (zip). Pass `--folder` to skip the entry attempt and force
    folder mode.
    """
    parts = args.strip().split()
    if not parts:
        print("usage: /download <entry_id|folder_id> [<local_path>] [--folder]")
        return

    force_folder = False
    if "--folder" in parts:
        force_folder = True
        parts = [p for p in parts if p != "--folder"]
    if not parts:
        print("missing id")
        return

    target_id = parts[0]
    dest_str = parts[1] if len(parts) > 1 else None

    # Try entry first unless --folder was passed
    if not force_folder:
        try:
            meta = await ctx.client.get_entry_metadata(target_id)
        except CliHttpError as e:
            meta = None
            if e.status != 404:
                print(f"download failed: HTTP {e.status} {e.payload}")
                return
        if meta is not None:
            dest = Path(dest_str) if dest_str else Path.cwd() / meta["display_name"]
            try:
                out = await ctx.client.download_entry(target_id, dest=dest)
            except CliHttpError as e:
                print(f"download failed: HTTP {e.status} {e.payload}")
                return
            print(f"saved {out['bytes_written']:,} bytes -> {out['saved_to']}")
            return

    # Fall back to folder zip
    dest = Path(dest_str) if dest_str else Path.cwd() / f"{target_id[:8]}.zip"
    if dest.is_dir():
        dest = dest / f"{target_id[:8]}.zip"
    try:
        out = await ctx.client.download_folder(target_id, dest=dest)
    except CliHttpError as e:
        print(f"folder download failed: HTTP {e.status} {e.payload}")
        return
    print(
        f"saved zip ({out['member_count']} files, "
        f"{out['bytes_written']:,} bytes) -> {out['saved_to']}"
    )


# ---- chat fallback --------------------------------------------------------

async def chat(ctx: CliContext, message: str) -> None:
    """Forward a non-slash message to the agent."""
    if ctx.session_id is None:
        out = await ctx.client.create_session(initiating_user_message=message)
        ctx.session_id = out["session_id"]
        print(f"(opened session {ctx.session_id})")

    try:
        with Spinner("调查员正在工作...").start() as _sp:
            result = await ctx.client.turn(ctx.session_id, message)
            _sp.finish("回答已就绪")
    except CliHttpError as e:
        print(f"turn failed: HTTP {e.status} {e.payload}")
        return

    print()
    print_markdown(result["agent_response"])
    print()
    usage = result.get("usage") or {}
    truncated = result.get("truncated")
    print(
        f"  [tokens in={usage.get('input_tokens', 0)} "
        f"out={usage.get('output_tokens', 0)} "
        f"tools={usage.get('tool_calls', 0)} "
        f"llm_calls={usage.get('llm_calls', 0)}]"
        + ("  ⚠ truncated" if truncated else "")
    )
    ctx.history.append({
        "user": message,
        "assistant": result["agent_response"],
        "conversation_id": result["conversation_id"],
    })


# ---- dispatch -------------------------------------------------------------

class _ExitREPL(Exception):
    """Raised by /quit to break out of the REPL loop."""


async def dispatch(ctx: CliContext, line: str) -> None:
    """Dispatch one input line. Slash command or chat."""
    line = line.strip()
    if not line:
        return
    if line.startswith("/"):
        rest = line[1:]
        name, args = _split_first(rest)
        handler = COMMANDS.get(name)
        if handler is None:
            print(f"unknown command: /{name}. try /help")
            return
        await handler(ctx, args)
    else:
        await chat(ctx, line)
