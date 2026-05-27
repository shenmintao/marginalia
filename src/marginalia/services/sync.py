"""Apply scan diffs.

Three operations the user can run after `/check`:

  - ingest_all_new(report)      Upload + ingest each disk-side new file.
  - apply_moved(report)         Update db rename/move to match disk.
  - forget_all_missing(report)  Soft-delete entries whose disk file is gone.

Each operation is independent and idempotent — safe to re-run after
partial failure. The /sync command does ingest_all_new + apply_moved +
forget_all_missing in one call.

Failure handling: each per-item failure is caught, rolled back, AND
collected into a `SyncFailure` returned to the caller. Previously
failures were only logged, so /ingest --all could report
"ingested=0 modified=0 ..." without any signal that 50 files quietly
failed mid-batch.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.engine import get_session_factory
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.models import File, FileEntry
from marginalia.services.entries import (
    move_entry,
    rename_entry,
    soft_delete_entry,
    _build_folder_display_path,
)
from marginalia.services.folders import resolve_or_create_folder
from marginalia.services.scan import ScanReport
from marginalia.services.upload import upload as upload_service
from marginalia.storage import MirrorStorage, get_storage
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

_PARALLEL_INGEST_LIMIT = 8


def _sha256_and_size(path: Path) -> tuple[str, int]:
    """Compute sha256 hex digest and file size (blocking — call via to_thread)."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while chunk := f.read(1024 * 256):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


@dataclass(slots=True)
class SyncFailure:
    """One per-item failure during apply_*. Surfaced to CLI for display."""
    category: str  # 'new' | 'moved' | 'modified' | 'missing'
    target: str    # path string or entry display_name
    error: str

async def adopt_disk_file(
    path: Path, vault_root: Path, folder_id: str | None = None,
) -> str:
    """Register a single disk-side file in the db without re-writing
    the bytes (file is already where mirror wants it). Returns the
    new entry_id.

    When `folder_id` is supplied (pre-resolved by ingest_all_new), the
    folder lookup/create step is skipped — avoids redundant DB queries
    when many files share the same parent folder.

    Raises on failure — callers that batch (`ingest_all_new`) catch and
    accumulate; CLI single-file caller surfaces the message directly.

    Used by both `/ingest --all` (called once per `report.new` entry)
    and `/ingest <path>` (single-file adoption from inside the vault).
    """
    storage = get_storage()
    if not isinstance(storage, MirrorStorage):
        raise RuntimeError(
            "adopt_disk_file is only meaningful when STORAGE_BACKEND=mirror"
        )

    rel = path.relative_to(vault_root).as_posix()
    display_name = path.relative_to(vault_root).parts[-1]
    size = path.stat().st_size
    sha256, _ = await asyncio.to_thread(_sha256_and_size, path)
    ext_pos = display_name.rfind(".")
    original_ext = display_name[ext_pos:].lower() if ext_pos != -1 else None
    mime_type = mimetypes.guess_type(display_name)[0]

    factory = get_session_factory()
    async with factory() as session:
        try:
            if folder_id is None:
                folder_segments = list(path.relative_to(vault_root).parts[:-1])
                folder = (
                    await resolve_or_create_folder(
                        session, segments=folder_segments,
                    )
                    if folder_segments else None
                )
                folder_id = folder.id if folder else None
            now = datetime.now(timezone.utc)
            file_id = new_id()
            file_row = File(
                id=file_id,
                storage_key=rel,
                sha256=sha256,
                size_bytes=size,
                mime_type=mime_type,
                original_ext=original_ext,
                kind="text",
                ingest_status="pending",
                created_at=now, updated_at=now,
            )
            session.add(file_row)
            await session.flush()

            entry = FileEntry(
                id=new_id(),
                folder_id=folder_id,
                file_id=file_id,
                display_name=display_name,
                lifecycle="active",
                created_at=now, updated_at=now,
            )
            session.add(entry)
            await session.flush()

            await audit_events_repo.append(session, kind="file_uploaded", payload={
                "file_id": file_id, "entry_id": entry.id,
                "display_name": display_name, "sha256": sha256,
                "size_bytes": size, "mime_type": mime_type,
                "source": "scan_adopt",
            })
            await enqueue(
                session, kind=KIND_INGEST_FILE,
                payload={"file_id": file_id, "entry_id": entry.id},
            )
            await session.commit()
            return entry.id
        except Exception:
            await session.rollback()
            raise

async def ingest_all_new(
    report: ScanReport,
    *,
    progress: "Callable[[int, int, Path], None] | None" = None,
) -> tuple[list[str], list[SyncFailure]]:
    """Register each disk-side new file in parallel.

    Pre-creates all folders upfront so files can be adopted without
    redundant folder lookups or commit-visibility races. Files are then
    adopted concurrently (bounded by _PARALLEL_INGEST_LIMIT).
    """
    # --- phase 0: pre-create all folders -------------------------------
    folder_map: dict[tuple[str, ...], str | None] = {}
    for path in report.new:
        segs = tuple(path.relative_to(report.vault_root).parts[:-1])
        if segs and segs not in folder_map:
            folder_map[segs] = None  # placeholder, resolved below

    if folder_map:
        factory = get_session_factory()
        async with factory() as session:
            for segs in folder_map:
                folder = await resolve_or_create_folder(session, segments=list(segs))
                folder_map[segs] = folder.id if folder else None
            await session.commit()

    # --- phase 1: adopt files in parallel ------------------------------
    created: list[str] = []
    failures: list[SyncFailure] = []
    total = len(report.new)
    done = 0
    sem = asyncio.Semaphore(_PARALLEL_INGEST_LIMIT)

    async def _adopt_one(path: Path) -> None:
        nonlocal done
        segs = tuple(path.relative_to(report.vault_root).parts[:-1])
        fid = folder_map.get(segs)
        async with sem:
            try:
                eid = await adopt_disk_file(
                    path, report.vault_root, folder_id=fid,
                )
                created.append(eid)
            except Exception as exc:  # noqa: BLE001
                log.error("ingest_all_new: failed for %s: %s", path, exc)
                failures.append(SyncFailure(
                    category="new",
                    target=str(path.relative_to(report.vault_root)),
                    error=f"{type(exc).__name__}: {exc}",
                ))
            done += 1
            if progress is not None:
                try:
                    progress(done, total, path)
                except Exception:
                    pass  # never let UI break the batch

    await asyncio.gather(*[_adopt_one(p) for p in report.new])
    return created, failures

async def apply_moved(
    report: ScanReport,
) -> tuple[int, list[SyncFailure]]:
    """For each entry whose disk file moved/renamed, update db to match.
    Returns (count_applied, failures).

    Key subtlety: the disk file is ALREADY at the new path (the user
    moved it externally). We need to update the file_row's storage_key
    to the new path BEFORE calling rename_entry / move_entry, so the
    mirror rename hook sees disk and db agree on current location and
    becomes a no-op move.
    """
    factory = get_session_factory()
    n = 0
    failures: list[SyncFailure] = []
    for entry, new_path in report.moved:
        rel = new_path.relative_to(report.vault_root).as_posix()
        new_segments = list(new_path.relative_to(report.vault_root).parts[:-1])
        new_name = new_path.relative_to(report.vault_root).parts[-1]

        async with factory() as session:
            live = await session.get(FileEntry, entry.id)
            if live is None or live.deleted_at is not None:
                continue
            file_row = await session.get(File, live.file_id)
            if file_row is None:
                continue
            file_row.storage_key = rel
            new_folder = (
                await resolve_or_create_folder(
                    session, segments=new_segments,
                )
                if new_segments else None
            )
            try:
                folder_changed = (
                    new_folder.id if new_folder else None
                ) != live.folder_id
                name_changed = live.display_name != new_name
                if folder_changed:
                    await move_entry(
                        session, entry_id=live.id,
                        new_folder_id=(
                            new_folder.id if new_folder else live.folder_id
                        ),
                    )
                    if name_changed:
                        await rename_entry(
                            session, entry_id=live.id,
                            new_name=new_name,
                        )
                elif name_changed:
                    await rename_entry(
                        session, entry_id=live.id,
                        new_name=new_name,
                    )
                else:
                    await session.commit()
                    continue
                await session.commit()
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.error("apply_moved: failed for entry=%s: %s",
                          entry.id, exc)
                await session.rollback()
                failures.append(SyncFailure(
                    category="moved",
                    target=f"{entry.display_name} → {rel}",
                    error=f"{type(exc).__name__}: {exc}",
                ))
    return n, failures

async def forget_all_missing(
    report: ScanReport,
) -> tuple[int, list[SyncFailure]]:
    """Soft-delete every entry the report flagged as missing on disk.
    Returns (count_applied, failures)."""
    factory = get_session_factory()
    n = 0
    failures: list[SyncFailure] = []
    for entry in report.missing:
        async with factory() as session:
            try:
                await soft_delete_entry(session, entry_id=entry.id)
                await session.commit()
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.error("forget_all_missing: failed for entry=%s: %s",
                          entry.id, exc)
                await session.rollback()
                failures.append(SyncFailure(
                    category="missing",
                    target=entry.display_name,
                    error=f"{type(exc).__name__}: {exc}",
                ))
    return n, failures

async def apply_modified(
    report: ScanReport,
) -> tuple[int, list[SyncFailure]]:
    """For entries whose disk file changed in place (same path, different
    sha256), update file_row.sha256/size and re-queue ingest. The entry
    keeps its identity (folder + display_name + entry_id stay the same)
    so callers / agents holding the entry_id don't lose context — only
    the indexed content gets refreshed. Returns (count_applied, failures).
    """
    factory = get_session_factory()
    n = 0
    failures: list[SyncFailure] = []
    for entry, path in report.modified:
        try:
            new_sha, size = await asyncio.to_thread(_sha256_and_size, path)
        except Exception as exc:  # noqa: BLE001
            log.error("apply_modified: hash failed for %s: %s", path, exc)
            failures.append(SyncFailure(
                category="modified",
                target=str(path.relative_to(report.vault_root)),
                error=f"{type(exc).__name__}: {exc}",
            ))
            continue

        async with factory() as session:
            try:
                live_entry = await session.get(FileEntry, entry.id)
                if live_entry is None or live_entry.deleted_at is not None:
                    continue
                file_row = await session.get(File, live_entry.file_id)
                if file_row is None:
                    continue
                file_row.sha256 = new_sha
                file_row.size_bytes = size
                file_row.ingest_status = "pending"
                file_row.summary = None
                file_row.description = None
                await enqueue(
                    session, kind=KIND_INGEST_FILE,
                    payload={
                        "file_id": file_row.id,
                        "entry_id": live_entry.id,
                    },
                )
                await session.commit()
                n += 1
            except Exception as exc:  # noqa: BLE001
                log.error("apply_modified: failed for entry=%s: %s",
                          entry.id, exc)
                await session.rollback()
                failures.append(SyncFailure(
                    category="modified",
                    target=entry.display_name,
                    error=f"{type(exc).__name__}: {exc}",
                ))
    return n, failures

async def apply_all(
    report: ScanReport,
    *,
    progress: "Callable[[int, int, Path], None] | None" = None,
) -> dict[str, object]:
    """Single entry point: ingest new + apply moved + apply modified +
    forget missing. Mirrors `git add -A` semantics — make db match disk
    in every category.

    `progress(done, total, current_path)` is forwarded to ingest_all_new
    (the only category slow enough to need a progress bar — others are
    folder/db updates and finish near-instantly even on big reports).

    Returns counts plus a `failures: list[SyncFailure]` so the caller
    can render per-item errors instead of silently reporting partial
    success."""
    (new_ids, new_failures), (moved, moved_failures), (modified, modified_failures), (forgotten, missing_failures) = await asyncio.gather(
        ingest_all_new(report, progress=progress),
        apply_moved(report),
        apply_modified(report),
        forget_all_missing(report),
    )
    return {
        "ingested": len(new_ids),
        "moved": moved,
        "modified": modified,
        "forgotten": forgotten,
        "failures": (
            new_failures + moved_failures
            + modified_failures + missing_failures
        ),
    }
