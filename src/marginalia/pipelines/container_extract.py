"""Container extraction helpers — shared by pipelines/container.py and
agent/tools/analyze_container.py.

Two responsibilities:

  1. Detect a container's flavor (zip / tar / tar.gz / git_repo).
  2. Extract members into a tempdir with hard safety limits and a clean
     filtered manifest. Used both at ingest time (to enumerate the
     container) and at agent time (to read inner files on demand).

Safety:
  - Path traversal: any member with absolute path or `..` segment is rejected.
  - Per-member size cap (default 4 MB).
  - Total uncompressed size cap (default 50 MB).
  - Compression-bomb guard: ratio of uncompressed/compressed > 100 → reject.
  - Member count cap (default 5000).

Ignore filters (default-applied):
  - `.git/` (the directory's contents, but the presence of `.git/HEAD`
    still flags the container as git_repo)
  - `node_modules/`, `.venv/`, `venv/`, `__pycache__/`, `dist/`, `build/`,
    `.next/`, `target/`
  - `*.lock`, `*.so`, `*.dll`, `*.dylib`, `*.pyc`
  - Hidden dotfiles at root other than `.gitignore` are kept (we want to
    surface README.md but ignore `.DS_Store` etc.) — actual rule: skip
    `.DS_Store`, `.git/` contents, `.idea/`, `.vscode/` directories.
"""
from __future__ import annotations

import io
import logging
import os
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_MEMBER_COUNT = 5_000
MAX_COMPRESSION_RATIO = 100


@dataclass(slots=True)
class ContainerMember:
    path: str           # POSIX-style relative path inside container
    size: int           # uncompressed bytes
    is_dir: bool


@dataclass(slots=True)
class ExtractResult:
    container_kind: str       # "zip_archive" | "tar_archive" | "git_repo"
    extract_root: Path        # tempdir holding extracted contents
    members: list[ContainerMember] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (path, reason)


_IGNORE_DIRS = {
    "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".next", "target", ".idea", ".vscode",
}
_IGNORE_FILE_EXTS = {".lock", ".so", ".dll", ".dylib", ".pyc", ".class"}
_IGNORE_FILE_NAMES = {".DS_Store", "Thumbs.db"}


def detect_kind(filename: str | None, mime: str | None) -> str | None:
    """Best-effort container kind detection. Returns None if not a
    recognised container shape."""
    name = (filename or "").lower()
    mt = (mime or "").lower()
    if name.endswith(".zip") or "zip" in mt:
        return "zip"
    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2")):
        return "tar"
    if "tar" in mt or "gzip" in mt:
        return "tar"
    return None


def _path_is_safe(member_path: str) -> bool:
    """Reject absolute paths and parent-traversal segments."""
    if not member_path:
        return False
    p = member_path.replace("\\", "/")
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return False
    parts = p.split("/")
    if any(seg == ".." for seg in parts):
        return False
    return True


_GIT_METADATA_PATHS = (
    ".git/HEAD",
    ".git/packed-refs",
    ".git/config",
)
_GIT_METADATA_PREFIXES = (
    ".git/refs/",
    ".git/logs/",
)


def _is_git_metadata_file(path: str) -> bool:
    """`.git/...` files we want extracted (so parse() can read them) but
    NOT listed as content members. parse() needs:
      - .git/HEAD
      - .git/refs/heads/<branch>
      - .git/logs/HEAD
      - .git/config
      - .git/packed-refs (for packed branch tips)
    """
    p = path.replace("\\", "/")
    if p in _GIT_METADATA_PATHS:
        return True
    return any(p.startswith(prefix) for prefix in _GIT_METADATA_PREFIXES)


def _is_ignored(member_path: str) -> bool:
    parts = member_path.replace("\\", "/").split("/")
    # always allow the .git directory marker so git detection works,
    # but skip its contents
    for i, seg in enumerate(parts[:-1]):
        if seg in _IGNORE_DIRS:
            return True
        if seg == ".git":
            return True
    fname = parts[-1]
    if fname in _IGNORE_FILE_NAMES:
        return True
    ext = os.path.splitext(fname)[1].lower()
    if ext in _IGNORE_FILE_EXTS:
        return True
    return False


def extract(
    body: bytes,
    *,
    extract_root: Path,
    member_cap: int = MAX_MEMBER_COUNT,
    member_byte_cap: int = MAX_MEMBER_BYTES,
    total_byte_cap: int = MAX_TOTAL_BYTES,
) -> ExtractResult:
    """Extract `body` into `extract_root`. Caller owns the tempdir.

    `body` may be a zip or a tar (autodetected by magic bytes)."""
    extract_root.mkdir(parents=True, exist_ok=True)

    if body[:2] == b"PK":
        kind = "zip"
    elif body[:6] == b"ustar\x00" or len(body) > 264 and body[257:262] == b"ustar":
        kind = "tar"
    elif body[:2] == b"\x1f\x8b":
        kind = "tar"  # gzip-wrapped tar
    elif body[:4] == b"BZh9":
        kind = "tar"
    else:
        # Last-ditch: zipfile / tarfile sniff
        kind = "zip" if body[:2] == b"PK" else "tar"

    if kind == "zip":
        return _extract_zip(body, extract_root, member_cap,
                            member_byte_cap, total_byte_cap)
    return _extract_tar(body, extract_root, member_cap,
                        member_byte_cap, total_byte_cap)


def _extract_zip(
    body: bytes, extract_root: Path,
    member_cap: int, member_byte_cap: int, total_byte_cap: int,
) -> ExtractResult:
    members: list[ContainerMember] = []
    skipped: list[tuple[str, str]] = []
    seen_git = False
    total_uncompressed = 0
    compressed = max(1, len(body))

    with zipfile.ZipFile(io.BytesIO(body), "r") as zf:
        infos = zf.infolist()[:member_cap + 100]
        for info in infos:
            if len(members) >= member_cap:
                skipped.append((info.filename, "member_cap"))
                continue
            if not _path_is_safe(info.filename):
                skipped.append((info.filename, "unsafe_path"))
                continue
            if info.filename.endswith("/"):
                # directory
                if info.filename.replace("\\", "/").rstrip("/").endswith(".git"):
                    seen_git = True
                continue
            if "/.git/" in "/" + info.filename or info.filename.startswith(".git/"):
                seen_git = True
                # Extract git metadata files to disk so parse() can read
                # them, but don't add them to `members` (the LLM-visible
                # content list).
                if _is_git_metadata_file(info.filename):
                    if info.file_size <= member_byte_cap:
                        target = extract_root / info.filename
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info, "r") as src, target.open("wb") as dst:
                            dst.write(src.read())
                    skipped.append((info.filename, "git_metadata_only"))
                    continue
            if _is_ignored(info.filename):
                skipped.append((info.filename, "ignored"))
                continue
            if info.file_size > member_byte_cap:
                skipped.append((info.filename, "too_large"))
                continue
            if total_uncompressed + info.file_size > total_byte_cap:
                skipped.append((info.filename, "total_cap"))
                continue
            if (total_uncompressed + info.file_size) / compressed > MAX_COMPRESSION_RATIO:
                skipped.append((info.filename, "compression_ratio"))
                continue
            target = extract_root / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                dst.write(src.read())
            members.append(ContainerMember(
                path=info.filename.replace("\\", "/"),
                size=info.file_size, is_dir=False,
            ))
            total_uncompressed += info.file_size
    container_kind = "git_repo" if seen_git else "zip_archive"
    return ExtractResult(
        container_kind=container_kind,
        extract_root=extract_root,
        members=members, skipped=skipped,
    )


def _extract_tar(
    body: bytes, extract_root: Path,
    member_cap: int, member_byte_cap: int, total_byte_cap: int,
) -> ExtractResult:
    members: list[ContainerMember] = []
    skipped: list[tuple[str, str]] = []
    seen_git = False
    total_uncompressed = 0
    compressed = max(1, len(body))

    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
        for info in tf:
            if len(members) >= member_cap:
                skipped.append((info.name, "member_cap"))
                continue
            if not info.isreg() and not info.isdir():
                skipped.append((info.name, "not_regular"))
                continue
            if not _path_is_safe(info.name):
                skipped.append((info.name, "unsafe_path"))
                continue
            if info.isdir():
                if info.name.replace("\\", "/").rstrip("/").endswith(".git"):
                    seen_git = True
                continue
            if "/.git/" in "/" + info.name or info.name.startswith(".git/"):
                seen_git = True
                if _is_git_metadata_file(info.name):
                    if info.size <= member_byte_cap:
                        extracted = tf.extractfile(info)
                        if extracted is not None:
                            target = extract_root / info.name
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with target.open("wb") as dst:
                                dst.write(extracted.read())
                    skipped.append((info.name, "git_metadata_only"))
                    continue
            if _is_ignored(info.name):
                skipped.append((info.name, "ignored"))
                continue
            if info.size > member_byte_cap:
                skipped.append((info.name, "too_large"))
                continue
            if total_uncompressed + info.size > total_byte_cap:
                skipped.append((info.name, "total_cap"))
                continue
            if (total_uncompressed + info.size) / compressed > MAX_COMPRESSION_RATIO:
                skipped.append((info.name, "compression_ratio"))
                continue
            target = extract_root / info.name
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(info)
            if extracted is None:
                skipped.append((info.name, "unreadable"))
                continue
            with target.open("wb") as dst:
                dst.write(extracted.read())
            members.append(ContainerMember(
                path=info.name.replace("\\", "/"),
                size=info.size, is_dir=False,
            ))
            total_uncompressed += info.size

    container_kind = "git_repo" if seen_git else "tar_archive"
    return ExtractResult(
        container_kind=container_kind,
        extract_root=extract_root,
        members=members, skipped=skipped,
    )


def directory_tree(members: list[ContainerMember],
                   max_depth: int = 2) -> dict[str, dict]:
    """Aggregate members into a depth-N nested directory dict for
    `description.tree`."""
    out: dict[str, dict] = {}
    for m in members:
        parts = m.path.split("/")
        if len(parts) == 1:
            continue  # top-level files don't go in `tree`
        top = parts[0] + "/"
        node = out.setdefault(top, {"file_count": 0, "kinds": set()})
        node["file_count"] += 1
        node["kinds"].add(_kind_of(m.path))
    # finalize sets to sorted lists
    for v in out.values():
        v["kinds"] = sorted(v["kinds"])
    return out


_EXT_TO_KIND = {
    ".py": "code", ".js": "code", ".ts": "code", ".tsx": "code",
    ".go": "code", ".rs": "code", ".java": "code", ".rb": "code",
    ".c": "code", ".cc": "code", ".cpp": "code", ".h": "code", ".hpp": "code",
    ".sh": "code", ".bash": "code",
    ".md": "text", ".rst": "text", ".txt": "text",
    ".json": "data", ".yaml": "data", ".yml": "data", ".toml": "data",
    ".csv": "table",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".svg": "image",
    ".pdf": "doc",
}


def _kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _EXT_TO_KIND.get(ext, "other")


KEY_FILE_PATTERNS = (
    "README.md", "README.rst", "README.txt", "README",
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Gemfile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "Makefile", "CHANGELOG.md", "LICENSE", "LICENSE.md",
)


def pick_key_files(members: list[ContainerMember],
                   limit: int = 6) -> list[ContainerMember]:
    """Choose a few significant files for the LLM to summarize.

    Strategy: top-level matches against KEY_FILE_PATTERNS first, then
    same patterns at any depth, then largest text/code files."""
    by_name: dict[str, ContainerMember] = {m.path: m for m in members}
    picked: list[ContainerMember] = []
    seen_paths: set[str] = set()
    for pat in KEY_FILE_PATTERNS:
        if pat in by_name and pat not in seen_paths:
            picked.append(by_name[pat])
            seen_paths.add(pat)
            if len(picked) >= limit:
                return picked
    for pat in KEY_FILE_PATTERNS:
        for m in members:
            if m.path.endswith("/" + pat) and m.path not in seen_paths:
                picked.append(m)
                seen_paths.add(m.path)
                if len(picked) >= limit:
                    return picked
    return picked
