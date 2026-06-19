"""Slash command registry for the Marginalia CLI.

Style: Claude Code-like. The user types `/<name> <args>` to invoke a
command; anything else is forwarded to the agent as chat.

Adding a command: write `async def cmd_xxx(ctx, args_str)` and register it
via the @command decorator. Help text is its docstring's first line.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, MutableMapping, cast

from marginalia.agent.types import ChatMode
from marginalia.cli.client import CliHttpError, MarginaliaClient
from marginalia.cli.render import (
    CYAN,
    DIM,
    RESET,
    Spinner,
    render_markdown,
    short_duration,
)


@dataclass
class CliContext:
    """Mutable per-REPL state."""
    client: MarginaliaClient
    session_id: str | None = None
    cwd_remote: str = "/"  # for resolving relative remote paths
    history: list[dict] = field(default_factory=list)
    chat_mode: ChatMode = "auto"
    storage_backend: str = "?"  # filled from /health at startup
    # Tab-completion caches. Populated as a side effect of user commands
    # (search/ls/info/discover) — completion only suggests entries the
    # user has already had on screen, mirroring shell-history intuition.
    seen_entry_ids: dict[str, str] = field(default_factory=dict)  # id -> display_name
    seen_folder_paths: set[str] = field(default_factory=set)
    seen_tags: set[str] = field(default_factory=set)


def remember_entry(ctx: CliContext, entry_id: str, display_name: str = "") -> None:
    if entry_id:
        ctx.seen_entry_ids[entry_id] = display_name or ctx.seen_entry_ids.get(entry_id, "")


def remember_folder_path(ctx: CliContext, path: str) -> None:
    if path:
        p = path if path.endswith("/") else path + "/"
        ctx.seen_folder_paths.add(p)


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


def _split_first_arg(arg_str: str) -> tuple[str, str]:
    parts = _split_args(arg_str)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:]).strip()


def _split_args(arg_str: str) -> list[str]:
    arg_str = arg_str.strip()
    if not arg_str:
        return []
    try:
        parts = shlex.split(arg_str, posix=False)
    except ValueError:
        parts = arg_str.split()
    return [_strip_matching_quotes(part) for part in parts]


def _strip_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


# ---- command implementations ----------------------------------------------

@command("help")
async def cmd_help(ctx: CliContext, args: str) -> None:
    """List available slash commands."""
    print("\nAvailable slash commands:")
    for name, doc in list_commands():
        print(f"  {name:<20} {doc}")
    print("\nAnything not starting with '/' is treated as chat with the agent.")
    print(f"current cwd: {ctx.cwd_remote!r}\n")
    print(f"current chat mode: {ctx.chat_mode}\n")


@command("mode")
async def cmd_mode(ctx: CliContext, args: str) -> None:
    """/mode [auto|quick|deep] - show or change the chat investigation mode."""
    value = args.strip().lower()
    if not value:
        print(f"current chat mode: {ctx.chat_mode}")
        return
    if value not in {"auto", "quick", "deep"}:
        print("usage: /mode auto|quick|deep")
        return
    ctx.chat_mode = cast(ChatMode, value)
    print(f"chat mode set to {ctx.chat_mode}")


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


@command("background")
async def cmd_background(ctx: CliContext, args: str) -> None:
    """List running and pending background tasks (ingest / reflect / tend / etc.)."""
    try:
        snap = await ctx.client.list_active_tasks(limit=30)
    except Exception as e:  # noqa: BLE001
        print(f"  (background query failed: {e})")
        return
    running = snap.get("running") or []
    pending = snap.get("pending") or []
    if not running and not pending:
        print("  idle (nothing on the queue)")
        return

    def _line(row: dict) -> str:
        kind = row.get("kind", "?")
        label = row.get("label") or ""
        age = int(row.get("age_s") or 0)
        attempts = int(row.get("attempts") or 0)
        age_str = short_duration(age)
        bits = [kind]
        if label:
            bits.append(label)
        bits.append(age_str)
        if attempts > 1:
            bits.append(f"retry {attempts}")
        return "  " + "  ".join(bits)

    if running:
        print(f"  {len(running)} running:")
        for row in running:
            print(_line(row))
    if pending:
        print(f"  {len(pending)} pending:")
        for row in pending:
            print(_line(row))


@command("bg")
async def cmd_bg(ctx: CliContext, args: str) -> None:
    """Alias for /background."""
    await cmd_background(ctx, args)


@command("ls")
async def cmd_ls(ctx: CliContext, args: str) -> None:
    """List folders + entries at root or under a folder id."""
    parent_id = args.strip() or None
    out = await ctx.client.list_folder(parent_id=parent_id)
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

    async def _walk(parent_id: str | None, depth: int, prefix: str, path: str) -> None:
        if depth > max_depth:
            return
        out = await ctx.client.list_folder(parent_id=parent_id)
        folders = out.get("folders") or []
        for i, f in enumerate(folders):
            last = i == len(folders) - 1
            connector = "└── " if last else "├── "
            print(f"{prefix}{connector}{f['name']}  ({f['id'][:8]}…)")
            child_path = f"{path}{f['name']}/"
            remember_folder_path(ctx, child_path)
            await _walk(
                f["id"], depth + 1, prefix + ("    " if last else "│   "),
                child_path,
            )

    print()
    await _walk(None, 0, "", "/")
    print()


@command("upload")
async def cmd_upload(ctx: CliContext, args: str) -> None:
    """/upload <local_path> <remote_path>  — upload a single file.

    remote_path:
      - trailing '/'         folder; display_name = local basename
      - includes a '.' (ext) folder + filename (display_name = last segment)
    Quote any path containing spaces (both local and remote)."""
    local, remote = _split_first_arg(args)
    if not local or not remote:
        print('usage: /upload <local_path> <remote_path>')
        print('  remote_path: trailing "/" = folder; with extension = filename')
        print('  quote paths with spaces:  /upload "~/My docs/x.pdf" "/papers/Y Z.pdf"')
        return

    full_remote = _resolve_remote(ctx, remote)

    # In mirror mode, reject local paths that already live inside the
    # vault — those should go through /ingest, which adopts in place.
    # Without this, /upload would re-write the bytes (collision-renaming
    # the existing on-disk file).
    from marginalia.config import get_settings
    from marginalia.storage import MirrorStorage, get_storage
    storage = get_storage()
    if isinstance(storage, MirrorStorage):
        from pathlib import Path as _P
        vault_root = _P(get_settings().mirror_vault_root).resolve()
        try:
            _P(local).resolve().relative_to(vault_root)
        except ValueError:
            pass  # outside vault → fine, proceed
        else:
            print(
                f"{local!r} is inside the vault.\n"
                f"  → /upload is for copying files INTO the vault. "
                f"Use /ingest {_P(local).resolve().relative_to(vault_root).as_posix()!r} "
                f"to register an existing vault file."
            )
            return

    try:
        out = await ctx.client.upload_file(
            local_path=local,
            remote_path=full_remote,
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
        remember_entry(ctx, e["entry_id"], e["display_name"])
        remember_folder_path(ctx, e["folder_path"])
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
    remember_entry(ctx, meta["entry_id"], meta["display_name"])
    remember_folder_path(ctx, meta.get("folder_path") or "")
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
    preview = meta.get("preview") or []
    if preview:
        print("  preview:")
        for sec in preview:
            title = sec.get("title") or "(untitled)"
            body = sec.get("summary") or ""
            print(f"    - {title}")
            if body:
                # Indent the body so it visually nests under its title.
                for line in body.splitlines() or [body]:
                    print(f"        {line}")
        print()


@command("discover")
async def cmd_discover(ctx: CliContext, args: str) -> None:
    """/discover <entry_id> [N] [--all]  — show entries the corpus has linked to it.

    Backed by random-walk-with-restart over the entry_relations graph
    (cooccurrence + tag overlap + citation co-citation). By default only
    LLM-vetted edges contribute — those are the relations vet_relations
    has confirmed are real. Pass --all to walk the unvetted graph too
    (useful for inspecting raw miner output before the /tend cycle has
    vetted it)."""
    parts = _split_args(args)
    if not parts:
        print("usage: /discover <entry_id> [top_k=8] [--all] [--vet]")
        return
    entry_id = parts[0]
    include_unvetted = "--all" in parts
    vet = "--vet" in parts
    numeric = [p for p in parts[1:] if p.isdigit()]
    top_k = int(numeric[0]) if numeric else 8
    try:
        out = await ctx.client.discover(
            entry_id,
            top_k=top_k,
            include_unvetted=include_unvetted,
            vet=vet,
        )
    except CliHttpError as e:
        print(f"discover failed: HTTP {e.status} {e.payload}")
        return
    vetting = out.get("vetting") if isinstance(out.get("vetting"), dict) else None
    if vetting:
        if vetting.get("queued"):
            task_id = str(vetting.get("task_id") or "?")
            print(
                f"queued relation vetting task {task_id[:8]}; "
                "re-run /discover after it finishes."
            )
        elif not vetting.get("candidates_available"):
            print("no unvetted direct relations need background vetting.")
    results = out.get("results") or []
    if not results:
        if include_unvetted:
            print(f"no relations recorded for {entry_id[:8]} yet "
                  f"(run /tend to populate signals).")
        else:
            print(
                f"no vetted relations for {entry_id[:8]}.\n"
                f"  raw signals may exist — try /discover {entry_id[:8]} --all\n"
                f"  to see them, or run /tend so vet_relations evaluates them."
            )
        return
    mode = " (unvetted)" if include_unvetted else ""
    print(f"\n  seed: {entry_id[:8]}…{mode}")
    for r in results:
        bar = "█" * max(1, int(round(r["score"] * 50)))
        direct = "*" if r.get("direct_edge_weight") else " "
        print(
            f"  {direct} {r['score']:.3f}  {bar:<50s}  "
            f"{r['entry_id'][:8]}…  {r['display_name']}"
        )
        remember_entry(ctx, r["entry_id"], r["display_name"])
    print(
        f"\n  {len(results)} related entries  "
        f"(* = direct edge from seed)"
    )


_REPROCESS_STATUSES = {"pending", "processing", "done", "failed"}
_REPROCESS_USAGE = (
    "usage: /reprocess failed | all [failed] | folder <folder_id> [failed] | "
    "catalog <catalog_id> [failed] | tag <tag_id> [failed] | "
    "file <file_id> | files <file_id>..."
)


def _parse_reprocess_status(raw: str) -> str:
    status = raw.strip().lower()
    if status not in _REPROCESS_STATUSES:
        raise ValueError(
            "status must be one of " + ", ".join(sorted(_REPROCESS_STATUSES))
        )
    return status


def _merge_reprocess_status(existing: str | None, raw: str) -> str:
    status = _parse_reprocess_status(raw)
    if existing is not None and existing != status:
        raise ValueError(f"conflicting status filters: {existing!r} and {status!r}")
    return status


def _pop_reprocess_status_options(parts: list[str]) -> tuple[list[str], str | None]:
    out: list[str] = []
    status: str | None = None
    i = 0
    while i < len(parts):
        part = parts[i]
        if part == "--status":
            i += 1
            if i >= len(parts):
                raise ValueError("--status requires a value")
            status = _merge_reprocess_status(status, parts[i])
        elif part.startswith("--status="):
            status = _merge_reprocess_status(status, part.split("=", 1)[1])
        else:
            out.append(part)
        i += 1
    return out, status


def parse_reprocess_parts(parts: list[str]) -> tuple[dict[str, Any], str]:
    parts, status = _pop_reprocess_status_options(parts)
    if not parts:
        if status is None:
            raise ValueError(_REPROCESS_USAGE)
        body: dict[str, Any] = {"status": status}
        return body, _reprocess_scope_label(body)

    first = parts[0].lower()
    if len(parts) == 1 and first in _REPROCESS_STATUSES:
        status = _merge_reprocess_status(status, first)
        body = {"status": status}
        return body, _reprocess_scope_label(body)

    tail: list[str] = []
    if first == "all":
        body = {"all": True}
        tail = parts[1:]
    elif first == "folder":
        if len(parts) < 2:
            raise ValueError("missing folder_id")
        body = {"folder_id": parts[1]}
        tail = parts[2:]
    elif first == "catalog":
        if len(parts) < 2:
            raise ValueError("missing catalog_id")
        body = {"catalog_id": parts[1]}
        tail = parts[2:]
    elif first == "tag":
        if len(parts) < 2:
            raise ValueError("missing tag_id")
        body = {"tag_id": parts[1]}
        tail = parts[2:]
    elif first in {"file", "files"}:
        ids = parts[1:]
        if ids and ids[-1].lower() in _REPROCESS_STATUSES:
            status = _merge_reprocess_status(status, ids.pop())
        if not ids:
            raise ValueError("missing file_id")
        body = {"file_ids": ids}
    elif len(parts) == 1:
        body = {"file_ids": [parts[0]]}
    else:
        raise ValueError(_REPROCESS_USAGE)

    for token in tail:
        if token.lower() in _REPROCESS_STATUSES:
            status = _merge_reprocess_status(status, token)
        else:
            raise ValueError(f"unexpected argument: {token}")
    if status is not None:
        body["status"] = status
    return body, _reprocess_scope_label(body)


def _reprocess_scope_label(body: dict[str, Any]) -> str:
    if body.get("all"):
        scope = "all files"
    elif body.get("folder_id"):
        scope = f"folder {body['folder_id']}"
    elif body.get("catalog_id"):
        scope = f"catalog {body['catalog_id']}"
    elif body.get("tag_id"):
        scope = f"tag {body['tag_id']}"
    elif body.get("file_ids"):
        count = len(body["file_ids"])
        scope = f"{count} file" + ("" if count == 1 else "s")
    else:
        scope = "all files"
    status = body.get("status")
    return f"{scope}, status={status}" if status else scope


def _print_reprocess_result(out: dict[str, Any], label: str) -> None:
    file_count = int(out.get("file_count") or 0)
    task_ids = out.get("task_ids") or []
    reused_count = int(out.get("reused_count") or 0)
    skipped_count = int(out.get("skipped_count") or 0)
    if file_count == 0:
        print(f"no matching files to reprocess ({label})")
        return
    print(
        f"reprocess queued: {file_count} file(s), {len(task_ids)} task(s)"
        f"  ({label})"
    )
    if reused_count or skipped_count:
        print(f"  reused={reused_count} skipped={skipped_count}")


@command("reprocess")
async def cmd_reprocess(ctx: CliContext, args: str) -> None:
    """Re-run ingest for files: failed, all, folder <id>, tag <id>, or file <id>."""
    try:
        body, label = parse_reprocess_parts(_split_args(args))
    except ValueError as e:
        print(str(e))
        if str(e) != _REPROCESS_USAGE:
            print(_REPROCESS_USAGE)
        return
    try:
        out = await ctx.client.reprocess_bulk(body)
    except CliHttpError as e:
        print(f"reprocess failed: HTTP {e.status} {e.payload}")
        return
    _print_reprocess_result(out, label)


@command("export")
async def cmd_export(ctx: CliContext, args: str) -> None:
    """/export [<conv_id>] [<dest>]  — export a conversation.

    Output format follows the destination suffix:
      *.md  → single markdown file with citations expanded inline
      *.zip → archive with report.md, manifest.json, and reference files
              (default if no extension given)

    Resolution order when conv_id is omitted:
      1. ctx.history's last conversation (this CLI's most recent chat)
      2. server's GET /conversations/latest (most recent ended conversation)
      3. error message if neither exists
    """
    parts = _split_args(args)
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
                print("usage: /export <conv_id> [<dest.md|.zip>]")
                return
            conv_id = latest["conversation_id"]
            preview = latest.get("user_message_preview") or ""
            print(f"(no id given; using server's most recent conversation: "
                  f"{conv_id[:8]}... \"{preview}\")")

    if dest_str is None:
        dest = Path.cwd() / f"conversation-{conv_id[:8]}.zip"
    else:
        dest = Path(dest_str)
    is_markdown = dest.suffix.lower() == ".md"
    try:
        if is_markdown:
            out = await ctx.client.export_conversation_markdown(conv_id, dest=dest)
        else:
            out = await ctx.client.export_conversation(conv_id, dest=dest)
    except CliHttpError as e:
        print(f"export failed: HTTP {e.status} {e.payload}")
        return
    fmt = "markdown" if is_markdown else "zip"
    print(
        f"exported {out['bytes_written']:,} bytes ({fmt}) -> {out['saved_to']}\n"
        f"  citations: {out['citation_count']} "
        f"(missing: {out['missing_count']})"
    )


@command("download")
async def cmd_download(ctx: CliContext, args: str) -> None:
    """/download <entry_id|folder_id> [<local_path>]  — file → bytes; folder → zip."""
    parts = _split_args(args)
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


@command("tend")
async def cmd_tend(ctx: CliContext, args: str) -> None:
    """Run a maintenance pass — `tend [run_id]` to check status, no args to start."""
    arg = args.strip()
    if arg:
        try:
            status = await ctx.client.tend_status(arg)
        except CliHttpError as e:
            print(f"tend status failed: HTTP {e.status} {e.payload}")
            return
        _print_tend_status(status)
        return

    try:
        out = await ctx.client.tend_start()
    except CliHttpError as e:
        print(f"tend failed: HTTP {e.status} {e.payload}")
        return
    run_id = out["tend_run_id"]
    print(f"tend run started: {run_id}")
    print(f"  {len(out['tasks'])} task(s) dispatched along the maintenance chain.")
    skipped = [t for t in out["tasks"] if t.get("skipped")]
    if skipped:
        print(
            f"  ({len(skipped)} reused an existing pending/running task — "
            "no duplicate work)"
        )
    for t in out["tasks"]:
        marker = "↺" if t.get("skipped") else "→"
        print(f"  {marker} {t['kind']}")
    print(
        f"\nthe librarian will work in the background. "
        f"check progress with `/tend {run_id}`."
    )


def _print_tend_status(status: dict) -> None:
    total = status.get("total", 0)
    settled = status.get("settled", 0)
    print(
        f"tend run {status['tend_run_id']}: "
        f"{settled}/{total} settled"
        + ("  ✓ all done" if status.get("all_settled") else "")
    )
    counts = status.get("state_counts") or {}
    parts = [f"{k}={v}" for k, v in counts.items() if v]
    if parts:
        print("  " + "  ".join(parts))
    for p in status.get("progress") or []:
        kind = p.get("kind", "?")
        st = p.get("status", "?")
        ts = p.get("finished_at") or p.get("started_at") or ""
        ts_short = ts[:19] if ts else ""
        err = f"  err: {p.get('last_error')[:80]}" if p.get("last_error") else ""
        print(f"  [{st:8}] {kind}  {ts_short}{err}")


# ---- chat fallback --------------------------------------------------------

_TOOL_LABELS = {
    "list_catalogs": "list_catalogs",
    "read_catalog": "read_catalog",
    "resolve_tag": "resolve_tag",
    "materialize_view": "materialize_view",
    "search_metadata": "search_metadata",
    "read_entries_metadata": "read_entries_metadata",
    "read_files": "read_files",
    "search_journal": "search_journal",
}


def _format_tool_call(name: str, arguments: dict) -> str:
    label = _TOOL_LABELS.get(name, name)
    if not arguments:
        return f"calling {label}"
    parts = []
    for k, v in arguments.items():
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
        if len(s) > 24:
            s = s[:21] + "..."
        parts.append(f"{k}={s}")
    inner = ", ".join(parts)
    if len(inner) > 60:
        inner = inner[:57] + "..."
    return f"calling {label}({inner})"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def _format_metrics(done_payload: dict, tool_count: int) -> str:
    """Claude-Code-style trailing line: (1m 54s · ↑ 2.9k / ↓ 2.9k tokens · 87% cache  · 2 tools)."""
    duration_ms = int(done_payload.get("duration_ms", 0) or 0)
    tokens_in = int(done_payload.get("tokens_in", 0) or 0)
    tokens_out = int(done_payload.get("tokens_out", 0) or 0)
    cache_read = int(done_payload.get("cache_read", 0) or 0)
    tools = int(done_payload.get("tool_calls", tool_count) or 0)

    parts = [short_duration(duration_ms / 1000.0)]
    parts.append(f"↑ {_fmt_tokens(tokens_in)} / ↓ {_fmt_tokens(tokens_out)} tokens")
    if cache_read and tokens_in:
        pct = round(cache_read / tokens_in * 100)
        parts.append(f"{pct}% cache")
    parts.append(f"{tools} tools")
    return "(" + " · ".join(parts) + ")"


def _plan_text_and_budget(data: str) -> tuple[str, dict | None]:
    try:
        payload = json.loads(data)
    except (ValueError, TypeError):
        return data, None
    if not isinstance(payload, dict):
        return data, None
    text = payload.get("text")
    budget = payload.get("budget")
    return (
        text if isinstance(text, str) else data,
        budget if isinstance(budget, dict) else None,
    )


def _format_budget_label(budget: dict | None) -> str | None:
    if not budget:
        return None
    tier = str(budget.get("tier") or "?")
    limit = budget.get("limit")
    mode = str(budget.get("mode") or "auto")
    if isinstance(limit, int):
        return f"{mode} budget: {tier} ({limit} rounds)"
    return f"{mode} budget: {tier}"


async def chat(ctx: CliContext, message: str) -> None:
    """Forward a non-slash message to the agent and render the SSE stream.

    Each phase commits its own line as it finishes, so the screen builds up
    a stable history (no in-place rewrites between phases):

        ⠋ planning the investigation...
            ⠋ calling search_journal(q="raft consensus")
            ⠋ calling read_files(entry_id=...)
            ⠋ investigator thinking...
            ✓ answer ready
        <answer markdown>
        (1m 54s · ↑ 2.9k / ↓ 2.9k tokens · 87% cache · 2 tools)
    """
    if ctx.session_id is None:
        out = await ctx.client.create_session(initiating_user_message=message)
        ctx.session_id = out["session_id"]
        print(f"(opened session {ctx.session_id})")

    # kb-lite-style flat layout: one indent column for every step, dim-grey
    # commit lines (no markers) so the trail reads like quiet log output.
    sp: Spinner | None = Spinner("planning the investigation...").start()

    def _swap(label: str) -> None:
        nonlocal sp
        if sp is not None:
            sp.finish()
        sp = Spinner(label).start()

    conversation_id: str | None = None
    plan_text: str = ""
    answer: str = ""
    done_payload: dict = {}
    error_msg: str | None = None
    tool_count = 0

    try:
        async for ev in ctx.client.stream_chat(
            ctx.session_id,
            message,
            mode=ctx.chat_mode,
        ):
            if ev.event_type == "conversation":
                conversation_id = ev.data
            elif ev.event_type == "planning":
                pass
            elif ev.event_type == "plan":
                plan_text, budget = _plan_text_and_budget(ev.data)
                # NO_PLAN fast-path: planner declared this turn trivial. Keep
                # the running spinner; the next event is `answer` which we
                # commit at the end. No verbose plan dump.
                if plan_text.lstrip().startswith("NO_PLAN:"):
                    if sp is not None:
                        sp.update("answering...")
                    continue
                if sp is not None:
                    sp.finish("planning ready")
                if plan_text.strip():
                    print()
                    budget_label = _format_budget_label(budget)
                    if budget_label:
                        print(f"  {CYAN}{budget_label}{RESET}")
                    print(f"  {CYAN}Plan:{RESET}")
                    for ln in plan_text.strip().split("\n"):
                        print(f"    {DIM}{ln}{RESET}")
                    print()
                sp = Spinner("investigator working...").start()
            elif ev.event_type == "thinking":
                try:
                    thinking = json.loads(ev.data)
                except (ValueError, TypeError):
                    thinking = {}
                if isinstance(thinking, dict) and thinking.get("budget_upgraded"):
                    limit = thinking.get("limit")
                    tier = thinking.get("budget_tier")
                    _swap(f"budget upgraded to {tier} ({limit} rounds)")
                else:
                    _swap("investigator thinking...")
            elif ev.event_type == "tool_call":
                tool_count += 1
                try:
                    payload = json.loads(ev.data)
                except (ValueError, TypeError):
                    payload = {}
                # Backend now sends a kb-lite-style `display` one-liner
                # with entry_ids resolved to display names. Use it when
                # present; older payloads fall through to the local
                # JSON-style formatter.
                display = payload.get("display")
                if display:
                    _swap(f"calling {display}")
                else:
                    _swap(_format_tool_call(
                        payload.get("name", "?"),
                        payload.get("arguments") or {},
                    ))
            elif ev.event_type == "tool_result":
                # Tool spinner stays open until the next phase event commits
                # it. Surface failure inline so the cause is visible.
                try:
                    payload = json.loads(ev.data)
                except (ValueError, TypeError):
                    payload = {}
                if not payload.get("ok", True) and sp is not None:
                    sp.fail(f"tool failed: {payload.get('error', '')[:40]}")
                    sp = None
            elif ev.event_type == "user_artifact":
                try:
                    art = json.loads(ev.data)
                except (ValueError, TypeError):
                    art = {}
                p = art.get("payload") or {}
                if p.get("kind") == "vega_lite" and sp is not None:
                    sp.update(
                        f"chart ready: {p.get('chart_id', '?')} - "
                        f"{(p.get('caption') or '')[:40]}"
                    )
                elif p.get("kind") == "data_export" and sp is not None:
                    sp.update(
                        f"export ready: {p.get('filename', '?')} "
                        f"({p.get('row_count', 0)} rows)"
                    )
            elif ev.event_type == "answer":
                answer = ev.data
            elif ev.event_type == "error":
                error_msg = ev.data
            elif ev.event_type == "done":
                try:
                    done_payload = json.loads(ev.data)
                except (ValueError, TypeError):
                    done_payload = {}
    except CliHttpError as e:
        if sp is not None:
            sp.fail(f"HTTP {e.status}")
        print(f"chat failed: {e.payload}")
        return

    if error_msg is not None:
        if sp is not None:
            sp.fail(error_msg)
        return

    if sp is not None:
        sp.finish("answer ready")
    print()
    for ln in render_markdown(answer, reserve_columns=4).split("\n"):
        print(f"    {ln}" if ln else "")
    print()
    truncated = bool(done_payload.get("truncated"))
    metrics = _format_metrics(done_payload, tool_count)
    suffix = "  ⚠ truncated" if truncated else ""
    print(f"  {DIM}{metrics}{RESET}{suffix}")
    print()
    if conversation_id:
        ctx.history.append({
            "user": message,
            "assistant": answer,
            "conversation_id": conversation_id,
        })


# ---- dispatch -------------------------------------------------------------

class _ExitREPL(Exception):
    """Raised by /quit to break out of the REPL loop."""


# ---- /check, /sync, /ingest --all, /forget --all-missing -----------------

@command("check")
async def cmd_check(ctx: CliContext, args: str) -> None:
    """/check  — diff the mirror vault against db (no writes).

    Walks <vault>, hashes each file, and reports new / modified / moved /
    missing entries vs the db. Read-only — apply with /ingest.
    """
    report = await _load_scan_report()
    if report is None:
        return
    from marginalia.services.scan import render_report
    print(render_report(report))


@command("ingest")
async def cmd_ingest(ctx: CliContext, args: str) -> None:
    """/ingest <vault_path> | --all  — make db match disk.

    Like `git add`: bring db in sync with the actual state of the file
    on disk. Handles four cases at once:
      - file is new on disk        → create entry + queue ingest
      - file content changed       → re-queue ingest (entry kept)
      - file moved/renamed         → update folder + display_name
      - file gone from disk        → soft-delete entry

    Single-path form acts on whatever category that path falls in.
    --all form applies every change /check reports. /upload is for
    copying a file from OUTSIDE the vault into it; /ingest is the
    in-vault counterpart.
    """
    from pathlib import Path
    from marginalia.config import get_settings
    from marginalia.storage import MirrorStorage, get_storage

    storage = get_storage()
    if not isinstance(storage, MirrorStorage):
        print(
            "/ingest only works with STORAGE_BACKEND=mirror.\n"
            "(local backend keeps files at UUID paths; nothing for the "
            "user to drop into a vault.)"
        )
        return

    parts = args.split()
    if not parts:
        print(
            "usage: /ingest <vault_path>   sync a single file\n"
            "       /ingest --all          sync the entire vault\n"
            "  copy a file from outside the vault → /upload"
        )
        return

    vault_root = Path(get_settings().mirror_vault_root).resolve()

    if "--all" in parts:
        report = await _load_scan_report()
        if report is None:
            return
        if report.total_changes == 0:
            print("nothing to do — vault is in sync with db.")
            return
        from marginalia.services.scan import render_report
        from marginalia.services.sync import apply_all
        print(render_report(report))
        if "--yes" not in parts:
            print(
                f"\napply {report.total_changes} changes? [y/N] ",
                end="",
            )
            try:
                confirm = input().strip().lower()
            except EOFError:
                confirm = ""
            if confirm not in ("y", "yes"):
                print("cancelled.")
                return
        def _progress(done: int, total: int, _path: Path) -> None:
            # Single-line progress redraw. Last update overwrites itself
            # so the terminal doesn't fill with N/M lines on big batches.
            import sys as _sys
            bar_w = 20
            filled = int(round(bar_w * done / total)) if total else bar_w
            bar = "█" * filled + "·" * (bar_w - filled)
            _sys.stdout.write(f"\r  ingesting [{bar}] {done}/{total}")
            _sys.stdout.flush()
            if done == total:
                _sys.stdout.write("\n")
                _sys.stdout.flush()

        out = await apply_all(report, progress=_progress)
        print(
            f"applied: ingested={out['ingested']} "
            f"modified={out['modified']} moved={out['moved']} "
            f"forgotten={out['forgotten']}"
        )
        failures = out.get("failures") or []
        if failures:
            print(f"\n{len(failures)} item(s) failed:")
            for f in failures:
                print(f"  [{f.category}] {f.target}: {f.error}")

        # Bulk ingest only admits the file (db row + bytes). Each entry
        # then sits in the task queue waiting on LLM extraction. Surface
        # the queue depth so users know how much work is pending.
        try:
            counts = await ctx.client.running_task_count()
            queued = counts.get("running", 0) + counts.get("pending", 0)
        except Exception:
            queued = 0
        if queued:
            print(
                f"\n{queued} ingest task(s) queued — they will run in the "
                f"background. The prompt shows live count as `N busy`."
            )
        return

    # Single-path form. Resolve, validate vault membership, route to
    # the right per-category handler.
    target_arg = parts[0]
    target = Path(target_arg)
    if not target.is_absolute():
        target = (vault_root / target_arg).resolve()
    else:
        target = target.resolve()
    try:
        target.relative_to(vault_root)
    except ValueError:
        print(
            f"{target_arg!r} is outside the vault ({vault_root}).\n"
            f"  → use /upload <local> <remote> to copy a file into the vault."
        )
        return
    if not target.is_file():
        print(
            f"not a file: {target}\n"
            f"  (single-path /ingest expects an existing vault file. "
            f"To clean up entries whose disk file is gone, run /ingest --all.)"
        )
        return

    # Route by classifying this one path. Cheap: hash the file, check db.
    from marginalia.services.sync import (
        adopt_disk_file, apply_modified, apply_moved,
    )
    from marginalia.services.scan import scan_vault, ScanReport
    full_report = await scan_vault(vault_root)
    rel = target.relative_to(vault_root).as_posix()

    # Match this path against each category in the full scan.
    new_match = next((p for p in full_report.new
                      if p.relative_to(vault_root).as_posix() == rel), None)
    mod_match = next(((e, p) for e, p in full_report.modified
                      if p.relative_to(vault_root).as_posix() == rel), None)
    moved_match = next(((e, p) for e, p in full_report.moved
                        if p.relative_to(vault_root).as_posix() == rel), None)

    if new_match is not None:
        try:
            eid = await adopt_disk_file(new_match, vault_root)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to ingest {rel}: {type(exc).__name__}: {exc}")
            return
        print(f"ingested {rel} → entry={eid[:8]}")
        return
    if mod_match is not None:
        # apply_modified expects a ScanReport — build a minimal one.
        sub = ScanReport(vault_root=vault_root,
                         modified=[mod_match])
        n, failures = await apply_modified(sub)
        if failures:
            print(f"failed to refresh {rel}: {failures[0].error}")
            return
        print(f"refreshed {rel} (entry stays, ingest re-queued; n={n})")
        return
    if moved_match is not None:
        sub = ScanReport(vault_root=vault_root,
                         moved=[moved_match])
        n, failures = await apply_moved(sub)
        if failures:
            print(f"failed to update path {rel}: {failures[0].error}")
            return
        print(f"updated db to match disk path {rel} (n={n})")
        return
    print(f"{rel} is already in sync.")


async def _load_scan_report():
    """Resolve vault root, walk it, return ScanReport. Mirror-only."""
    from marginalia.config import get_settings
    from marginalia.services.scan import scan_vault
    from marginalia.storage import MirrorStorage, get_storage
    from pathlib import Path

    storage = get_storage()
    if not isinstance(storage, MirrorStorage):
        print(
            "/check + /sync only work with STORAGE_BACKEND=mirror.\n"
            "(local backend keeps files at UUID paths — there's nothing "
            "for the user to scan in Finder.)"
        )
        return None
    settings = get_settings()
    return await scan_vault(Path(settings.mirror_vault_root))


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
