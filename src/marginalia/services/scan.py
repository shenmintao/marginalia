"""Vault scan + sync service.

Walks the mirror vault on disk, sha256-hashes each file, joins with
the live `files` rows in db, and categorises every (file_entry, disk
file) pair as one of:

  - in_sync    Disk and db agree on path + hash. Most common.
  - new        File on disk not referenced by any live entry.
  - missing    Live entry references a path with nothing on disk.
  - moved      Hash matches an entry but the path differs (rename or
               relocate; the difference between the two is which field
               changed and is implicit from the path comparison).

Only scans live (deleted_at IS NULL) entries. Soft-deleted entries
are not considered missing — if their disk file is gone, that's fine
(the user soft-deleted; cleanup happens at purge time).

This is read-only. It NEVER writes to db; the apply step is a separate
service used by /sync, /ingest --all, /forget --all-missing.
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.engine import get_session_factory
from marginalia.db.models import File, FileEntry
from marginalia.repositories import entries as entries_repo
from marginalia.services.entries import _build_folder_display_path


@dataclass(slots=True)
class ScanReport:
    new: list[Path] = field(default_factory=list)
    missing: list[FileEntry] = field(default_factory=list)
    moved: list[tuple[FileEntry, Path]] = field(default_factory=list)
    modified: list[tuple[FileEntry, Path]] = field(default_factory=list)
    in_sync_count: int = 0
    vault_root: Path = field(default_factory=Path)

    @property
    def total_changes(self) -> int:
        return (
            len(self.new) + len(self.missing)
            + len(self.moved) + len(self.modified)
        )


async def scan_vault(vault_root: Path) -> ScanReport:
    """Walk the vault, hash everything, diff against the db."""
    vault_root = vault_root.resolve()
    if not vault_root.is_dir():
        raise NotADirectoryError(f"vault root not found: {vault_root}")

    disk_files = await asyncio.to_thread(_walk_and_hash, vault_root)
    # disk_files: dict[relpath, sha256]

    factory = get_session_factory()
    async with factory() as s:
        live_entries = await entries_repo.list_live_with_file(s)

        report = ScanReport(vault_root=vault_root)
        seen_disk_paths: set[str] = set()

        for entry, file_row in live_entries:
            folder_display = await _build_folder_display_path(
                s, entry.folder_id,
            )
            expected_rel = (
                f"{folder_display.lstrip('/')}/{entry.display_name}"
                if folder_display else entry.display_name
            )
            disk_sha = disk_files.get(expected_rel)
            if disk_sha == file_row.sha256:
                report.in_sync_count += 1
                seen_disk_paths.add(expected_rel)
                continue
            if disk_sha is not None:
                # Same path, different content: user edited in place.
                # Treat as a re-ingest target rather than missing+new
                # so the report stays readable.
                report.modified.append((entry, vault_root / expected_rel))
                seen_disk_paths.add(expected_rel)
                continue
            # Hash matches some other path? It's a move/rename.
            mover = next(
                (p for p, h in disk_files.items()
                 if h == file_row.sha256 and p not in seen_disk_paths),
                None,
            )
            if mover:
                report.moved.append((entry, vault_root / mover))
                seen_disk_paths.add(mover)
            else:
                report.missing.append(entry)

        # Disk paths nobody claimed = new uploads pending.
        for rel, _sha in disk_files.items():
            if rel not in seen_disk_paths:
                report.new.append(vault_root / rel)

    return report


def _walk_and_hash(vault_root: Path) -> dict[str, str]:
    """Sync helper — hashes everything under vault_root. Run inside
    asyncio.to_thread so we don't block the loop."""
    out: dict[str, str] = {}
    for path in vault_root.rglob("*"):
        if not path.is_file():
            continue
        # Skip hidden files / dotfiles to keep the report focused on
        # user content. Users who want them indexed can /upload them
        # explicitly.
        if any(part.startswith(".") for part in path.relative_to(vault_root).parts):
            continue
        rel = path.relative_to(vault_root).as_posix()
        h = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1024 * 256):
                h.update(chunk)
        out[rel] = h.hexdigest()
    return out


def render_report(report: ScanReport) -> str:
    lines = [
        f"vault: {report.vault_root}",
        f"  in_sync:  {report.in_sync_count}",
        f"  new:      {len(report.new)}",
        f"  modified: {len(report.modified)}",
        f"  missing:  {len(report.missing)}",
        f"  moved:    {len(report.moved)}",
    ]
    if report.new:
        lines.append("\n[new] disk files not in db:")
        for p in report.new[:50]:
            rel = p.relative_to(report.vault_root)
            lines.append(f"  + {rel}  ({p.stat().st_size:,} B)")
        if len(report.new) > 50:
            lines.append(f"  … {len(report.new) - 50} more")
    if report.modified:
        lines.append("\n[modified] same path, content changed:")
        for entry, path in report.modified[:50]:
            rel = path.relative_to(report.vault_root)
            lines.append(f"  ~ {rel}  (entry={entry.id[:8]})")
        if len(report.modified) > 50:
            lines.append(f"  … {len(report.modified) - 50} more")
    if report.missing:
        lines.append("\n[missing] db entries with no disk file:")
        for e in report.missing[:50]:
            lines.append(f"  - {e.display_name}  (entry={e.id[:8]})")
        if len(report.missing) > 50:
            lines.append(f"  … {len(report.missing) - 50} more")
    if report.moved:
        lines.append("\n[moved] hash matched at a different path:")
        for entry, new_path in report.moved[:50]:
            rel = new_path.relative_to(report.vault_root)
            lines.append(f"  ~ {entry.display_name} → {rel}")
        if len(report.moved) > 50:
            lines.append(f"  … {len(report.moved) - 50} more")
    if report.total_changes > 0:
        lines.append(
            "\nApply with /ingest --all  "
            "(or /ingest <path> for one file)"
        )
    return "\n".join(lines)
