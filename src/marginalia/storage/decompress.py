"""Archive walking — uniform py7zz backend.

Single primitive: `open_archive(body, filename)` — a context manager that
extracts an archive into a managed tempdir, yields an `ArchiveSession`
that gives both a member listing and random-access reads, and cleans
up on exit. Both ArchivePipeline (peek + member-dispatched reads) and
the agent's `analyze_container` tool build on this.

Supported formats: anything py7zz/7zz can open — zip, tar, tar.*, 7z,
rar, .gz/.bz2/.xz (single-member compressors look like 1-member archives
in this view), iso, cab, plus the rest of py7zz's 50+ format coverage.

Bomb defense:
  - py7zz's internal SecurityConfig (file-count, ratio) trips during
    extraction
  - we additionally enforce a cumulative byte cap *post*-extraction
    (200 MB default) by summing member sizes after walking the dir,
    cleaning up and raising before yielding the session

Slow archives are fine — ingest runs in the background. We don't try
to parallelize: 7zz already pipelines extraction internally.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

DEFAULT_BOMB_LIMIT_BYTES = 200 * 1024 * 1024


class DecompressionError(RuntimeError):
    """Raised when archive walk fails or trips the bomb cap."""


# ---- compression / archive detection -------------------------------------
# Filename hint helpers — used by upload metadata and routing. Routing
# itself sends every recognised archive shape to ArchivePipeline.

_SINGLE_FILE_SUFFIXES = (
    ".gz", ".gzip", ".bz2", ".bzip2", ".xz", ".lzma",
)
_COMPOUND_ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
)
_PLAIN_ARCHIVE_SUFFIXES = (
    ".zip", ".tar", ".7z", ".rar", ".iso", ".cab",
)


def detect_compression(filename: str) -> tuple[str, str | None]:
    """Split a filename into (inner_name, compression_method).

    Compound archive suffixes are kept whole. The result is **only used
    as a hint** — routing sends every archive-shaped file to
    ArchivePipeline regardless. Kept for the few places (upload
    metadata, /info display) that want to surface "user uploaded a .gz
    wrapping a .pdf" without re-parsing the name.

    >>> detect_compression('raft.pdf.gz')
    ('raft.pdf', 'gz')
    >>> detect_compression('access.log.bz2')
    ('access.log', 'bz2')
    >>> detect_compression('backup.tar.gz')
    ('backup.tar.gz', None)
    >>> detect_compression('source.tgz')
    ('source.tgz', None)
    >>> detect_compression('raft.pdf')
    ('raft.pdf', None)
    """
    lower = filename.lower()
    for compound in _COMPOUND_ARCHIVE_SUFFIXES:
        if lower.endswith(compound):
            return filename, None
    suffix_to_method = {
        ".gz": "gz", ".gzip": "gz",
        ".bz2": "bz2", ".bzip2": "bz2",
        ".xz": "xz", ".lzma": "xz",
    }
    for suffix, method in suffix_to_method.items():
        if lower.endswith(suffix):
            return filename[: -len(suffix)], method
    return filename, None


def is_archive_suffix(filename: str) -> bool:
    """True if this filename should be routed to ArchivePipeline."""
    lower = filename.lower()
    if lower.endswith(_COMPOUND_ARCHIVE_SUFFIXES):
        return True
    if lower.endswith(_PLAIN_ARCHIVE_SUFFIXES):
        return True
    if lower.endswith(_SINGLE_FILE_SUFFIXES):
        return True
    return False


# ---- archive session -----------------------------------------------------

@dataclass(slots=True, frozen=True)
class ArchiveMember:
    """One regular file inside an archive. `path` is a posix-style
    relative path from the archive root."""
    path: str
    size: int


class ArchiveSession:
    """Random-access view of an extracted archive. Bound to a tempdir
    that is cleaned up when the parent context exits — never hold an
    ArchiveSession past the `with open_archive(...)` block.

    `unsafe_basenames` lists basenames whose original archive entry had
    a path-traversal component (e.g. `../escape.txt`). py7zz silently
    rewrites those on extract; ArchivePipeline / analyze_container drop
    matching members from the listing.
    """
    __slots__ = ("root", "members", "unsafe_basenames", "_by_path")

    def __init__(
        self,
        root: Path,
        members: list[ArchiveMember],
        *,
        unsafe_basenames: set[str] | None = None,
    ):
        self.root = root
        self.members = members
        self.unsafe_basenames = unsafe_basenames or set()
        self._by_path = {m.path: m for m in members}

    def has(self, path: str) -> bool:
        return path in self._by_path

    def get(self, path: str) -> ArchiveMember | None:
        return self._by_path.get(path)

    def read_bytes(self, path: str) -> bytes:
        """Read one member's full bytes. Raises DecompressionError if
        the path is not in the archive."""
        member = self._by_path.get(path)
        if member is None:
            raise DecompressionError(f"member not found: {path!r}")
        return (self.root / path).read_bytes()

    def iter_bytes(self) -> Iterator[tuple[str, bytes]]:
        """Yield (path, bytes) for every member, in listing order."""
        for member in self.members:
            yield member.path, (self.root / member.path).read_bytes()


@contextmanager
def open_archive(
    body: bytes,
    filename: str,
    *,
    bomb_limit_bytes: int = DEFAULT_BOMB_LIMIT_BYTES,
) -> Iterator[ArchiveSession]:
    """Extract `body` (treated as an archive of the shape implied by
    `filename`) into a tempdir and yield an ArchiveSession. Cleans up
    the tempdir on context exit — even on exceptions.

    Trips DecompressionError before yielding if total extracted size
    exceeds `bomb_limit_bytes` (post-extraction safety net layered on
    top of py7zz's internal SecurityConfig).
    """
    try:
        import py7zz
    except ImportError as exc:  # pragma: no cover
        raise DecompressionError(
            "py7zz is required for archive support — pip install py7zz"
        ) from exc

    tmp_root = Path(tempfile.mkdtemp(prefix="marginalia-archive-"))
    archive_path = tmp_root / _safe_archive_name(filename)
    extract_dir = tmp_root / "out"
    extract_dir.mkdir()

    try:
        archive_path.write_bytes(body)
        # Pre-flight listing — py7zz silently sanitises path-traversal
        # entries on extract (rewrites ../escape.txt → escape.txt). To
        # let downstream filters reject those without losing the rest
        # of the archive, we collect the list of unsafe basenames and
        # propagate them via the session.
        unsafe_basenames: set[str] = set()
        # Best-effort decompression-bomb guard BEFORE writing anything to
        # disk: classic bombs honestly declare a huge uncompressed size (the
        # trick is the ratio), so if the listing exposes per-entry sizes we
        # refuse up front. When no size metadata is available we fall back to
        # the post-extraction walk below. Note: list_archive() returns plain
        # name strings in py7zz 1.3.x, so sizes must come from
        # SevenZipFile.infolist() (ArchiveInfo.file_size).
        declared_total = 0
        have_sizes = False
        try:
            with py7zz.SevenZipFile(str(archive_path), "r") as _sz:
                listing = _sz.infolist()
        except Exception as exc:
            raise DecompressionError(
                f"py7zz could not list {filename!r}: {exc}"
            ) from exc
        for entry in listing:
            name = (
                getattr(entry, "filename", None)
                or getattr(entry, "name", None)
                or (entry if isinstance(entry, str) else "")
            )
            if not name:
                continue
            _size = getattr(entry, "file_size", None)
            if isinstance(_size, int) and _size >= 0:
                declared_total += _size
                have_sizes = True
            n = str(name).replace("\\", "/")
            parts = n.split("/")
            if any(seg == ".." for seg in parts) or \
               n.startswith("/") or (len(n) > 1 and n[1] == ":"):
                # This entry will be silently rewritten by py7zz —
                # remember its sanitised basename so we can filter.
                unsafe_basenames.add(parts[-1] if parts else "")

        if have_sizes and declared_total > bomb_limit_bytes:
            raise DecompressionError(
                f"declared decompressed size {declared_total:,} bytes exceeds "
                f"{bomb_limit_bytes:,} (possible decompression bomb)"
            )

        try:
            py7zz.extract_archive(str(archive_path), str(extract_dir))
        except Exception as exc:
            raise DecompressionError(
                f"py7zz failed to extract {filename!r}: {exc}"
            ) from exc

        members: list[ArchiveMember] = []
        total = 0
        for path in sorted(_walk_files(extract_dir)):
            size = path.stat().st_size
            total += size
            if total > bomb_limit_bytes:
                raise DecompressionError(
                    f"decompressed output exceeded {bomb_limit_bytes:,} "
                    "bytes (possible decompression bomb)"
                )
            rel = path.relative_to(extract_dir).as_posix()
            members.append(ArchiveMember(path=rel, size=size))

        # `.tar.gz` / `.tar.bz2` / `.tar.xz` arrives as a 1-member shell
        # around an inner `.tar` — py7zz only undoes one layer at a
        # time. Detect this case and recurse so callers see the actual
        # tar members, not "bundle.tar".
        if len(members) == 1 and members[0].path.lower().endswith(".tar"):
            inner_path = extract_dir / members[0].path
            inner_body = inner_path.read_bytes()
            shutil.rmtree(extract_dir, ignore_errors=True)
            extract_dir = tmp_root / "out_inner"
            extract_dir.mkdir()
            # Use just the basename: members[0].path may include a
            # subdirectory (e.g. 'sub/foo.tar'), and tmp_root/'sub' does not
            # exist yet, so writing tmp_root/members[0].path would raise
            # FileNotFoundError.
            inner_archive = tmp_root / os.path.basename(members[0].path)
            inner_archive.write_bytes(inner_body)
            try:
                py7zz.extract_archive(str(inner_archive), str(extract_dir))
            except Exception as exc:
                raise DecompressionError(
                    f"py7zz failed to extract inner tar: {exc}"
                ) from exc
            members = []
            total = 0
            for path in sorted(_walk_files(extract_dir)):
                size = path.stat().st_size
                total += size
                if total > bomb_limit_bytes:
                    raise DecompressionError(
                        f"decompressed output exceeded "
                        f"{bomb_limit_bytes:,} bytes "
                        "(possible decompression bomb)"
                    )
                rel = path.relative_to(extract_dir).as_posix()
                members.append(ArchiveMember(path=rel, size=size))

        yield ArchiveSession(
            extract_dir, members, unsafe_basenames=unsafe_basenames,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def iter_archive_members(
    body: bytes,
    filename: str,
    *,
    bomb_limit_bytes: int = DEFAULT_BOMB_LIMIT_BYTES,
) -> Iterator[tuple[str, bytes]]:
    """Convenience: open + iterate + close. For callers that only need
    one pass and don't want to hold a session."""
    with open_archive(
        body, filename, bomb_limit_bytes=bomb_limit_bytes,
    ) as session:
        yield from session.iter_bytes()


def _walk_files(root: Path) -> Iterator[Path]:
    for dirpath, _dirs, files in os.walk(root):
        dp = Path(dirpath)
        for name in files:
            p = dp / name
            # Skip symlink members. Following them can crash on a dangling
            # target (stat -> FileNotFoundError) and, for an absolute symlink
            # like /etc/passwd that py7zz rewrites into the extract tree, would
            # read outside the archive. Archive symlinks aren't indexable
            # content, so dropping them here also keeps them out of the member
            # list (read_bytes/iter_bytes never follow them).
            if p.is_symlink():
                continue
            yield p


def _safe_archive_name(filename: str) -> str:
    """Strip any directory components the user might have included; keep
    the suffix so py7zz picks the right format."""
    return os.path.basename(filename) or "archive"
