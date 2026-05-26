"""ArchivePipeline (DESIGN.md §11.4 — unified archive model).

Replaces the old ContainerPipeline. Treats every archive shape as the
same thing: zip / tar / tar.* / 7z / rar / .gz / .bz2 / .xz / iso / cab.
Single-member compressors (`.pdf.gz`, `.log.gz`) are 1-member archives
in this view.

`run` (ingest) does NOT recursively ingest inner files as separate file
rows. Instead, for each picked member it asks the appropriate leaf
pipeline for a `read_segment_from_bytes(default_peek_args)` snapshot;
the resulting peeks plus a directory tree go to ONE summary LLM call.
The agent later drills into specific members via `read_segment(member_path=...)`
(which dispatches to that leaf pipeline) or the `analyze_container` tool.

Image members are a deliberate exception: ingest does NOT call the VLM
on archive-internal images — a single archive could carry hundreds and
that gets expensive fast. The agent can still drill in later.

Bomb defense + cleanup: handled by `open_archive` (storage/decompress.py).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from marginalia.llm import (
    ChatMessage, ChatRequest, TextBlock, get_chat_client,
)
from marginalia.llm.tagged_response import (
    parse_kv,
    parse_path,
    parse_tagged,
    parse_tags,
    render_format_hint,
)
from marginalia.pipelines.base import (
    Pipeline, PipelineContext, PipelineResult, SegmentResult, TagSuggestion,
)
from marginalia.pipelines.registry import register_pipeline, resolve_pipeline
from marginalia.storage import open_archive
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)


# Per-member peek limits — keep prompt budget bounded regardless of how
# many members there are.
PEEK_PER_MEMBER_CHARS = 1500
PEEK_MEMBERS_MAX = 8
PEEK_MEMBERS_MAX_FOR_TINY = 16  # if every member's tiny, show more
TREE_MAX_LINES = 200
TREE_DEPTH_MAX = 4

# Member filtering — applied post-extraction so the rest of the
# pipeline (and the agent) doesn't see noise members. Mirrors the old
# container_extract rules so existing tests + agent behaviour hold.
_IGNORE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "target", ".idea", ".vscode",
}
_IGNORE_FILE_EXTS = {".lock", ".so", ".dll", ".dylib", ".pyc", ".class"}
_IGNORE_FILE_NAMES = {".DS_Store", "Thumbs.db"}

# Inside a `.git/` tree we keep only the metadata files that
# git_metadata.parse needs; everything else is filtered.
_GIT_METADATA_FILES = (
    ".git/HEAD", ".git/packed-refs", ".git/config",
)
_GIT_METADATA_PREFIXES = (
    ".git/refs/", ".git/logs/",
)


# Common ext → mime guesses, just enough to route members through
# resolve_pipeline. resolve_pipeline matches on filename too, so this
# table only needs the long tail of "no recognised mime, generic ext".
_EXT_TO_MIME: dict[str, str] = {
    ".txt": "text/plain", ".md": "text/markdown", ".rst": "text/x-rst",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".html": "text/html", ".htm": "text/html",
    ".log": "application/x-log",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
    ".py": "text/x-python", ".js": "text/javascript", ".ts": "text/typescript",
    ".json": "application/json", ".yaml": "text/yaml", ".yml": "text/yaml",
    ".toml": "text/x-toml", ".xml": "text/xml",
}


ARCHIVE_PIPELINE_SYSTEM = """You are Marginalia's archive indexer.

You receive a directory tree summary and short peeks of a few member
files inside an archive (zip / tar / 7z / rar / .gz / etc.). Produce a
structured index that lets a downstream agent decide whether to retrieve
the archive and find the relevant inner file.

`summary` (2-4 sentences in the dominant language) describes what the
archive contains and what kind of artefact it looks like. `description`
is a free-text walk-through of the archive's organisation — directory
layout, notable subprojects, anything that helps the agent navigate.

`extra` carries archive-specific machine-readable insights as `key:
value` lines (one per line). Use these keys when applicable:
  archive_kind: zip | tar.gz | 7z | gz | ... (use the file_extension hint)
  primary_language: python | javascript | go | ... (only if it looks
    like a code repo / source bundle; omit otherwise)
  frameworks_detected: comma-separated list, evidence-based
Add other keys you find useful. Leave the block empty if there is
nothing notable.

`entry_extra` is the same shape but for position-aware insights.
`entry_catalog_path` is a best-guess classification path. `tags` are
3-10 facet:name pairs; valid facets are topic | form | time | source |
language | extra.

Do NOT speculate beyond what the tree and peeks show.

""" + render_format_hint(kinds=("container",))


# Schema kept for legacy callers but no longer fed to the LLM.
ARCHIVE_PIPELINE_SCHEMA: dict[str, Any] = {}


@register_pipeline(
    mimes=(
        "application/zip",
        "application/x-tar",
        "application/gzip", "application/x-gzip",
        "application/x-bzip2",
        "application/x-xz",
        "application/x-7z-compressed",
        "application/vnd.rar", "application/x-rar-compressed",
        "application/x-iso9660-image",
    ),
    exts=(
        ".zip", ".tar",
        ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
        ".gz", ".bz2", ".xz", ".lzma",
        ".7z", ".rar", ".iso", ".cab",
    ),
)
class ArchivePipeline(Pipeline):
    name = "archive"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        body = await _read_all(storage, ctx.storage_key)
        filename = _filename_from_ctx(ctx)
        archive_kind = _guess_archive_kind(filename)

        with open_archive(body, filename) as session:
            all_members = list(session.members)
            unsafe = session.unsafe_basenames
            members = [m for m in all_members if _is_listable(m.path, unsafe)]
            tree = _directory_tree(members)
            picks = _pick_members(members)
            peeks: list[dict[str, Any]] = []
            for member in picks:
                peeks.append(await _peek_member(session, member))

            # Git-repo detection + metadata. Surface in description so
            # the agent can see branch / recent commits / authors.
            container_kind, git_meta_dict = _detect_git(session, archive_kind)

            indexed_files = [
                {"path": m.path, "size": m.size}
                for m in members[:200]
            ]
            key_files = [
                {"path": p["path"], "size": next(
                    (m.size for m in members if m.path == p["path"]), 0,
                )}
                for p in peeks
            ]

            payload = {
                "archive_kind": archive_kind,
                "container_kind": container_kind,
                "filename": filename,
                "file_count": len(members),
                "total_uncompressed_bytes": sum(m.size for m in members),
                "tree": tree,
                "member_peeks": peeks,
                "git_metadata": git_meta_dict,
                "folder_path": ctx.folder_path,
                "sibling_names": ctx.sibling_names,
                "catalog_sketch": ctx.catalog_sketch,
                "tag_vocabulary": ctx.tag_vocabulary,
            }

        client = get_chat_client("ingest")
        resp = await client.complete(ChatRequest(
            system=ARCHIVE_PIPELINE_SYSTEM,
            messages=[ChatMessage(role="user", content=[TextBlock(text=(
                "Index the archive described below. The peeks are the only "
                "ground truth for member content.\n\n"
                f"<context>\n{json.dumps(payload, ensure_ascii=False)}\n</context>"
            ))])],
            max_tokens=4096,
            temperature=0.2,
        ))

        tagged = parse_tagged(resp.text or "")
        summary = tagged.get("summary", "").strip()
        if not summary:
            log.warning(
                "archive pipeline: no <summary> in response. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("archive pipeline produced empty summary")

        extra_kv = parse_kv(tagged.get("extra", ""))
        primary_language = extra_kv.pop("primary_language", "") or None
        frameworks_raw = extra_kv.pop("frameworks_detected", "")
        frameworks_detected = [
            f.strip() for f in frameworks_raw.split(",") if f.strip()
        ] if frameworks_raw else []
        # archive_kind from LLM extra is advisory; keep our local guess as fallback.
        llm_archive_kind = extra_kv.pop("archive_kind", "") or archive_kind

        description = {
            "archive_kind": llm_archive_kind,
            "container_kind": container_kind,
            "file_count": payload["file_count"],
            "total_uncompressed_bytes": payload["total_uncompressed_bytes"],
            "primary_language": primary_language,
            "frameworks_detected": frameworks_detected,
            "tree": tree,
            "indexed_files": indexed_files,
            "key_files": key_files,
            "git_metadata": git_meta_dict,
            "member_peeks": [
                {"path": p["path"], "kind": p["kind"], "preview": p["preview"][:400]}
                for p in peeks
            ],
        }
        description_text = tagged.get("description", "").strip()
        if description_text:
            description["text"] = description_text

        # Surviving extra keys + the original prose body (if any) become the
        # entry's `extra`. Re-render kv as one-per-line so storage reads back the
        # same shape.
        remaining_extra = "\n".join(
            f"{k}: {v}" for k, v in extra_kv.items()
        ) or None

        return PipelineResult(
            summary=summary,
            description=description,
            kind="container",
            extra=remaining_extra,
            entry_extra=tagged.get("entry_extra", "").strip() or None,
            entry_catalog_path=parse_path(tagged.get("catalog_path", "")) or None,
            entry_tags=[
                TagSuggestion(name=t["name"], facet=t["facet"])
                for t in parse_tags(tagged.get("tags", ""))
            ],
        )

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        member_path = (args.get("member_path") or "").strip()
        if not member_path:
            return SegmentResult(
                error="archive read_segment needs member_path; "
                      "use list_files or analyze_container to discover paths",
            )
        body = await _read_all(storage, file_row.storage_key)
        # Pull the original upload name from any visible FileEntry — py7zz
        # uses the suffix to pick the format and (for single-member shells)
        # to derive inner member names. Fall back to original_ext.
        filename = await _resolve_archive_filename(file_row)
        with open_archive(body, filename) as session:
            unsafe = session.unsafe_basenames
            visible = {
                m.path for m in session.members
                if _is_listable(m.path, unsafe)
            }
            if member_path not in visible:
                return SegmentResult(
                    error=f"member not found: {member_path!r}",
                    extras={"available": sorted(visible)[:50]},
                )
            data = session.read_bytes(member_path)
            inner = _resolve_inner(member_path)
            if inner is None:
                return SegmentResult(
                    error=f"no pipeline can read {member_path!r}",
                )
            inner_args = {k: v for k, v in args.items() if k != "member_path"}
            return await inner.read_segment_from_bytes(
                data, inner_args, filename=member_path,
            )


# ---- helpers -------------------------------------------------------------

async def _read_all(storage: StorageBackend, key: str) -> bytes:
    buf = bytearray()
    async for chunk in storage.get(key):
        buf.extend(chunk)
    return bytes(buf)


async def _resolve_archive_filename(file_row) -> str:
    """For read_segment: look up the entry's original display_name so
    py7zz sees the right suffix. Falls back to original_ext."""
    try:
        from marginalia.db.engine import get_session_factory
        from marginalia.repositories import entries as entries_repo
        factory = get_session_factory()
        async with factory() as s:
            row = await entries_repo.find_first_display_name_for_file(s, file_row.id)
            if row:
                return os.path.basename(row)
    except Exception:  # noqa: BLE001
        pass
    if file_row.original_ext:
        return f"archive{file_row.original_ext}"
    return "archive"


def _is_listable(path: str, unsafe_basenames: set[str] | None = None) -> bool:
    """Filter applied to py7zz output so noise + path-traversal doesn't
    show up in description.indexed_files / member_peeks / search.

    `unsafe_basenames` carries basenames whose original archive entry
    used `..` path-traversal segments — py7zz silently sanitises those
    on extract, so the only signal left at the listing stage is the
    pre-extraction name. Members whose basename is in this set are
    rejected unconditionally.
    """
    if not path:
        return False
    p = path.replace("\\", "/")
    parts = p.split("/")
    if any(seg == ".." for seg in parts):
        return False
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return False
    if unsafe_basenames and parts[-1] in unsafe_basenames:
        return False
    # Inside .git/, keep only the metadata files git_metadata.parse needs.
    if parts[0] == ".git":
        if p in _GIT_METADATA_FILES:
            return True
        return any(p.startswith(prefix) for prefix in _GIT_METADATA_PREFIXES)
    for seg in parts[:-1]:
        if seg in _IGNORE_DIRS:
            return False
    fname = parts[-1]
    if fname in _IGNORE_FILE_NAMES:
        return False
    ext = os.path.splitext(fname)[1].lower()
    if ext in _IGNORE_FILE_EXTS:
        return False
    return True


def _detect_git(session, archive_kind: str) -> tuple[str, dict | None]:
    """If the archive contains a `.git/` tree, identify it as a git_repo
    and parse the metadata; otherwise fall back to a generic kind name.

    Returns (container_kind, git_metadata_dict_or_None).
    """
    has_git = any(
        m.path == ".git/HEAD" or m.path.startswith(".git/")
        for m in session.members
    )
    if not has_git:
        # Map archive_kind → container_kind for legacy field shape.
        if archive_kind in ("zip",):
            return "zip_archive", None
        if archive_kind in ("tar", "tar.gz", "tgz", "tar.bz2", "tbz2",
                            "tar.xz", "txz"):
            return "tar_archive", None
        return archive_kind or "archive", None
    try:
        from marginalia.pipelines.git_metadata import parse as parse_git
        meta = parse_git(session.root)
        return "git_repo", (meta.to_dict() if meta else None)
    except Exception as exc:  # noqa: BLE001
        log.warning("git metadata parse failed: %s", exc)
        return "git_repo", None


def _filename_from_ctx(ctx: PipelineContext) -> str:
    """Best-effort filename for routing — py7zz uses the suffix to pick
    a decoder, and for single-member archives (.gz/.bz2/.xz) it also
    derives the inner member's name from the archive basename. We pass
    the original upload display_name when available so member names look
    natural ('access.log' inside 'access.log.gz') rather than UUIDs.
    """
    if ctx.display_name:
        return os.path.basename(ctx.display_name)
    base = os.path.basename(ctx.storage_key) or "archive"
    if ctx.original_ext and not base.endswith(ctx.original_ext):
        base = base + ctx.original_ext
    return base


def _guess_archive_kind(filename: str) -> str:
    lower = filename.lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz",
                   ".tgz", ".tbz2", ".txz",
                   ".7z", ".rar", ".zip", ".tar",
                   ".iso", ".cab", ".gz", ".bz2", ".xz", ".lzma"):
        if lower.endswith(suffix):
            return suffix.lstrip(".")
    return "archive"


def _resolve_inner(member_path: str):
    """Map a member path to a leaf pipeline. resolve_pipeline already
    handles ext_patterns + ext_overrides_mime, so we just give it a
    decent mime guess + the filename."""
    ext = os.path.splitext(member_path)[1].lower()
    mime = _EXT_TO_MIME.get(ext)
    return resolve_pipeline(mime, ext, filename=member_path)


def _pick_members(members) -> list:
    """Choose which members to peek. Strategy:
      - skip files that are tiny markers (<32 B) or huge (>2 MB) without
        having any cap; we still let the inner peek decide
      - prefer text-shaped, doc-shaped, log-shaped, and config files
      - cap to PEEK_MEMBERS_MAX (or _FOR_TINY if all are small)
    """
    if not members:
        return []
    PRIORITY = {
        ".md": 0, ".rst": 0, ".txt": 1,
        ".pdf": 1, ".docx": 1, ".xlsx": 2,
        ".log": 2,
        ".py": 3, ".js": 3, ".ts": 3, ".go": 3, ".rs": 3, ".java": 3,
        ".json": 4, ".yaml": 4, ".yml": 4, ".toml": 4,
        ".html": 5, ".csv": 5,
    }
    INTEREST_PATHS = (
        "readme", "license", "changelog", "pyproject", "package.json",
        "cargo.toml", "go.mod",
    )

    def member_score(m) -> tuple[int, int]:
        ext = os.path.splitext(m.path)[1].lower()
        base = os.path.basename(m.path).lower()
        prio = PRIORITY.get(ext, 9)
        bonus = -2 if any(name in base for name in INTEREST_PATHS) else 0
        return (prio + bonus, m.size)

    sorted_members = sorted(members, key=member_score)
    cap = PEEK_MEMBERS_MAX_FOR_TINY \
        if all(m.size < 4096 for m in members) else PEEK_MEMBERS_MAX
    return sorted_members[:cap]


async def _peek_member(session, member) -> dict[str, Any]:
    """Run the appropriate leaf pipeline's read_segment_from_bytes with
    default peek args. Skip VLM cost on archive-internal images."""
    path = member.path
    inner = _resolve_inner(path)
    if inner is None:
        return {
            "path": path,
            "kind": "unknown",
            "preview": f"[unknown type, {member.size} bytes]",
        }
    if inner.name == "image":
        # Bypass VLM here — placeholder only. Agent can drill later.
        return {
            "path": path,
            "kind": "image",
            "preview": f"[image: {os.path.basename(path)}, "
                       f"~{max(1, member.size // 1024)} KB]",
        }
    try:
        body = session.read_bytes(path)
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "kind": inner.name,
                "preview": f"[read failed: {exc}]"}

    args = {"max_chars": PEEK_PER_MEMBER_CHARS}
    try:
        result = await inner.read_segment_from_bytes(body, args, filename=path)
    except Exception as exc:  # noqa: BLE001
        return {"path": path, "kind": inner.name,
                "preview": f"[peek failed: {exc}]"}

    text = (result.text or "").strip()
    if not text and result.error:
        text = f"[{result.error}]"
    return {
        "path": path,
        "kind": inner.name,
        "preview": text[:PEEK_PER_MEMBER_CHARS] or "[empty]",
    }


def _directory_tree(members) -> str:
    """Render a compact ASCII tree, capped by depth and total lines.
    Same shape the old container.directory_tree produced."""
    if not members:
        return ""
    nodes: dict[tuple[str, ...], int] = {}
    for m in members:
        parts = tuple(p for p in m.path.split("/") if p)
        if not parts:
            continue
        for i in range(1, len(parts) + 1):
            sub = parts[:i]
            if i < len(parts):
                # directory marker
                nodes.setdefault(sub + ("/",), 0)
            else:
                nodes[sub] = m.size

    lines: list[str] = []
    for key in sorted(nodes.keys()):
        depth = len(key) - 1 if key[-1] == "/" else len(key) - 1
        if depth > TREE_DEPTH_MAX:
            continue
        prefix = "  " * depth
        last = key[-1]
        if last == "/":
            lines.append(f"{prefix}{key[-2]}/")
        else:
            size = nodes[key]
            lines.append(f"{prefix}{last}  ({size:,} B)")
        if len(lines) >= TREE_MAX_LINES:
            lines.append(f"… ({len(nodes) - len(lines)} more entries)")
            break
    return "\n".join(lines)
