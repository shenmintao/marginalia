"""Mirror storage backend — folder-tree on disk that matches user intent.

Storage layout under MARGINALIA_HOME/library/:

    research/llm/paper.pdf
    notes/2026-05/meeting.md
    photos/IMG_001.jpg
    bundle.tar.gz

Storage_key in the db is the relative posix path (e.g.
'research/llm/paper.pdf'). Sanitization happens at put-time and
collisions are resolved with ' (1)', ' (2)' suffixes — the same
numbering convention as services.upload._resolve_display_name so
disk basename and DB display_name stay identical.

This backend is the new default for local installs because:
  - users can browse / open / rsync / git the vault directly
  - no UUID indirection means the vault survives marginalia removal
  - ingest stays uniform: pipelines call storage.get(key) and don't
    care which backend is in play

Trade-offs vs local UUID-flat:
  - dedup is OFF (handled at the upload service layer based on
    backend type) — same bytes uploaded twice = two files
  - rename / move costs an extra disk op (transactional with db)
  - unicode / cross-platform filename quirks need sanitize()
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterator

import aiofiles
import aiofiles.os

from marginalia.storage.base import StorageBackend
from marginalia.storage.sanitize import sanitize_folder, sanitize_name

_CHUNK = 1024 * 256
_COLLISION_LIMIT = 10_000  # sanity cap


class MirrorStorage(StorageBackend):
    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, key: str) -> Path:
        # Defence-in-depth: refuse keys that escape the vault root.
        # storage_key is supposed to come out of put()/rename(), where
        # we control sanitization, but verifying here means a corrupt
        # db row can't trick us into reading /etc/passwd.
        candidate = (self.root / key).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError(
                f"storage_key {key!r} escapes vault root"
            ) from exc
        return candidate

    async def put(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        *,
        size: int | None = None,
        content_type: str | None = None,
        display_name: str | None = None,
        folder_path: str | None = None,
    ) -> str:
        # Mirror ignores `key` and computes a path from the hint pair.
        # If the caller didn't give a display_name we fall back to the
        # `key` they suggested — that lets accidental old call-sites
        # still write something sensible (UUID basename).
        safe_folder = sanitize_folder(folder_path or "")
        safe_name = sanitize_name(
            display_name or os.path.basename(key) or "unnamed"
        )
        dir_abs = self._abs(safe_folder)
        dir_abs.mkdir(parents=True, exist_ok=True)
        # Unique temp name per put: concurrent uploads of the same
        # display_name must not share one .part file (they would
        # interleave/truncate each other). Dot prefix keeps it out of
        # vault scans.
        tmp = dir_abs / f".part-{uuid.uuid4().hex}"
        claimed: Path | None = None
        try:
            async with aiofiles.open(tmp, "wb") as f:
                async for chunk in stream:
                    await f.write(chunk)
            # Claim the final name atomically: O_CREAT|O_EXCL loses the
            # race to whoever created the target first, so we retry with
            # the next ' (N)' suffix instead of overwriting their bytes.
            for candidate in _candidate_names(safe_name):
                rel = _join(safe_folder, candidate)
                target = self._abs(rel)
                try:
                    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    continue
                os.close(fd)
                claimed = target
                os.replace(tmp, target)
                return rel
            raise RuntimeError(
                f"could not resolve mirror path collision for "
                f"{display_name!r} after {_COLLISION_LIMIT} attempts"
            )
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            # If the O_EXCL claim succeeded but os.replace did not, the
            # claim is a zero-byte file squatting on the user-visible
            # final name — remove it too or it occupies the name forever.
            if claimed is not None:
                try:
                    os.unlink(claimed)
                except FileNotFoundError:
                    pass
            raise

    async def get(self, key: str) -> AsyncIterator[bytes]:
        async with aiofiles.open(self._abs(key), "rb") as f:
            while True:
                chunk = await f.read(_CHUNK)
                if not chunk:
                    return
                yield chunk

    async def get_range(self, key: str, start: int, end: int) -> bytes:
        length = max(0, end - start + 1)
        async with aiofiles.open(self._abs(key), "rb") as f:
            await f.seek(start)
            return await f.read(length)

    async def delete(self, key: str) -> None:
        try:
            await aiofiles.os.remove(self._abs(key))
        except FileNotFoundError:
            pass

    async def exists(self, key: str) -> bool:
        return await aiofiles.os.path.isfile(self._abs(key))

    async def rename(self, old_key: str, new_key: str) -> str:
        """Move on disk. `new_key` is a relative path, possibly with
        the desired display_name embedded; we re-sanitize and resolve
        collisions just like put().

        The disk work is fully synchronous (stat/mkdir/os.replace), so
        it runs in a worker thread — otherwise a large relocation would
        stall the event loop for its whole duration."""
        return await asyncio.to_thread(self.rename_sync, old_key, new_key)

    def rename_sync(self, old_key: str, new_key: str) -> str:
        """Synchronous core of rename() — callable from worker threads
        (services.folders batches subtree relocations off the loop)."""
        old_abs = self._abs(old_key)
        new_rel = _resolve_path(
            display_name=os.path.basename(new_key),
            folder_path=os.path.dirname(new_key),
            existing=self._exists_sync,
            skip=old_key,
            same_file=self._samefile_sync,
        )
        if new_rel == old_key:
            return old_key
        new_abs = self._abs(new_rel)
        new_abs.parent.mkdir(parents=True, exist_ok=True)
        if (
            new_rel.casefold() == old_key.casefold()
            and self._samefile_sync(old_key, new_rel)
        ):
            # Case-only rename on a case-insensitive filesystem: the
            # target "exists" as the same file and a direct os.replace
            # can be a no-op, so go through a temp name. Build the
            # destination without resolve() — resolve() echoes the
            # on-disk (old) casing for existing paths on Windows.
            dest = self.root.resolve() / new_rel
            tmp = old_abs.parent / f".case-{uuid.uuid4().hex}"
            os.replace(old_abs, tmp)
            try:
                os.replace(tmp, dest)
            except BaseException:
                os.replace(tmp, old_abs)  # roll back to the old name
                raise
        else:
            if old_abs == new_abs:
                return old_key
            os.replace(old_abs, new_abs)
        # best-effort cleanup of newly empty parents
        try:
            old_abs.parent.relative_to(self.root)
            for parent in old_abs.parents:
                if parent == self.root:
                    break
                try:
                    parent.rmdir()
                except OSError:
                    break
        except ValueError:
            pass
        return new_rel

    def _exists_sync(self, rel: str) -> bool:
        return self._abs(rel).exists()

    def _samefile_sync(self, a: str, b: str) -> bool:
        try:
            return os.path.samefile(self._abs(a), self._abs(b))
        except (OSError, ValueError):
            return False

    def relocate_entry_sync(self, old_key: str, new_rel: str) -> str | None:
        """Failure-tolerant single-entry relocation for folder-subtree
        sync. Returns the entry's new storage_key, or None to leave the
        row untouched.

        Unlike rename_sync it never raises FileNotFoundError for a
        missing source: the user may have deleted the file in Finder (a
        first-class mirror state that scan_vault reconciles later), or a
        previous partial relocation already moved it to the destination —
        in which case we adopt that destination key instead of failing."""
        old_abs = self._abs(old_key)
        if not old_abs.exists():
            # Source gone. If the intended (un-suffixed) destination
            # already holds a file, a prior partial run moved it there —
            # adopt it. Otherwise leave the row as-is for scan to flag.
            intended = _resolve_path(
                display_name=os.path.basename(new_rel),
                folder_path=os.path.dirname(new_rel),
                existing=lambda rel: False,
            )
            if intended != old_key and self._exists_sync(intended):
                return intended
            return None
        return self.rename_sync(old_key, new_rel)

    def rename_dir_case_sync(self, old_rel: str, new_rel: str) -> bool:
        """Two-step temp rename of a directory whose path differs from
        `new_rel` only by case. Returns True if a rename happened.

        Best effort: a no-op (returns False) when the source dir is gone
        or the filesystem is case-sensitive — there old_rel and new_rel
        are genuinely different directories, so os.path.samefile fails
        and the per-file relocation handles the move instead. On a
        case-insensitive fs (macOS/Windows) the two casings are the same
        physical directory, which a per-file move cannot re-case; renaming
        the directory itself keeps the on-disk casing in step with the
        storage_keys so scan does not diverge forever."""
        old_abs = self._abs(old_rel)
        if not old_abs.is_dir():
            return False
        if not self._samefile_sync(old_rel, new_rel):
            return False
        # Build the destination literally (not via _abs, which resolve()s
        # to the existing on-disk casing on Windows/macOS).
        dest = self.root.resolve() / new_rel
        tmp = old_abs.parent / f".casedir-{uuid.uuid4().hex}"
        os.replace(old_abs, tmp)
        try:
            os.replace(tmp, dest)
        except BaseException:
            os.replace(tmp, old_abs)  # roll back to the old casing
            raise
        return True

    def relocate_subtree_sync(
        self, pairs: list[tuple[str, str]],
    ) -> tuple[dict[str, str], dict[str, Exception]]:
        """Relocate a batch of (old_key, new_rel) entries after a folder
        rename/move. Runs entirely off the event loop (services.folders
        calls it via asyncio.to_thread) because every step is a blocking
        stat / mkdir / os.replace — doing it inline would stall the API
        and SSE loop for the whole relocation.

        Failure-tolerant: a missing source is skipped and a destination
        that already holds the file is adopted (see relocate_entry_sync),
        so a half-finished relocation is safe to retry. Returns:
          - results: {old_key: new_key} for every entry to repoint;
          - errors:  {old_key: exc} for UNEXPECTED per-entry failures,
            collected so the caller attempts every entry before failing.
        """
        results: dict[str, str] = {}
        errors: dict[str, Exception] = {}
        # 1. Case-only directory renames first. On a case-insensitive fs
        #    a per-file move cannot re-case the parent directory, so
        #    'docs'->'Docs' would leave the dir cased 'docs' while the
        #    keys say 'Docs'. Rename shallowest dirs first; nested
        #    descendants ride along with their ancestor's rename.
        handled_dirs: list[str] = []
        dir_pairs = sorted(
            {
                (os.path.dirname(o), os.path.dirname(n))
                for o, n in pairs
                if os.path.dirname(o) != os.path.dirname(n)
                and os.path.dirname(o).casefold() == os.path.dirname(n).casefold()
            },
            key=lambda p: p[0].count("/"),
        )
        for old_dir, new_dir in dir_pairs:
            if any(old_dir == h or old_dir.startswith(h + "/") for h in handled_dirs):
                handled_dirs.append(old_dir)
                continue
            try:
                self.rename_dir_case_sync(old_dir, new_dir)
                handled_dirs.append(old_dir)
            except OSError:
                # Best-effort (finding is LOW/partial): leave the file
                # relocation to run; scan reconciles any residual casing.
                pass
        # 2. Per-entry file relocation.
        for old_key, new_rel in pairs:
            try:
                new_key = self.relocate_entry_sync(old_key, new_rel)
                if new_key is not None:
                    results[old_key] = new_key
            except Exception as exc:  # noqa: BLE001 — collected, not swallowed
                errors[old_key] = exc
        return results, errors


def _candidate_names(safe_name: str) -> Iterator[str]:
    """Yield safe_name, then 'stem (N)ext' for N=1.. — the same numbering
    convention as services.upload._resolve_display_name so disk and DB
    names agree."""
    yield safe_name
    stem, dot, ext = safe_name.rpartition(".")
    if not dot:
        stem, ext = safe_name, ""
    else:
        ext = f".{ext}"
    for n in range(1, _COLLISION_LIMIT):
        yield f"{stem} ({n}){ext}"


def _resolve_path(
    *,
    display_name: str,
    folder_path: str | None,
    existing,
    skip: str | None = None,
    same_file=None,
) -> str:
    """Build a sanitized relative path for a display_name in folder_path,
    appending ' (N)' before the extension on collision until a free slot
    is found. `existing(rel)` returns True if the path is taken; pass
    `skip` to ignore a known-current path (used by rename). `same_file(a,
    b)` lets a casefold-equal variant of `skip` match too — on
    case-insensitive filesystems (Windows/macOS) the case-only rename
    target "exists" but is still our own file."""
    safe_folder = sanitize_folder(folder_path or "")
    safe_name = sanitize_name(display_name)

    def _is_skip(rel: str) -> bool:
        if skip is None:
            return False
        if rel == skip:
            return True
        return (
            same_file is not None
            and rel.casefold() == skip.casefold()
            and same_file(rel, skip)
        )

    for candidate in _candidate_names(safe_name):
        rel = _join(safe_folder, candidate)
        if _is_skip(rel) or not existing(rel):
            return rel
    raise RuntimeError(
        f"could not resolve mirror path collision for {display_name!r} "
        f"after {_COLLISION_LIMIT} attempts"
    )


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name
