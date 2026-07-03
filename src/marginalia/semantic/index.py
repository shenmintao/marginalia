from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import math
import os
import sqlite3
import sys
import struct
import time
import weakref
from array import array
from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Protocol

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.config import get_settings
from marginalia.db.models import File, FileEntry
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import files as files_repo
from marginalia.semantic.embeddings import EmbeddingResult, get_embedding_client


INDEX_VERSION = 1
DEFAULT_INDEX_NAME = "default"
SQLITE_VEC_INDEX_FILENAME = "vectors.sqlite"
# Cap the per-entry text handed to the embedding API. A long PDF whose
# description accumulated many chunked sections can otherwise exceed the
# provider token limit and make the whole batch (and thus the build) fail.
EMBEDDING_TEXT_MAX_CHARS = 6000


class EmbeddingClient(Protocol):
    async def embed(
        self,
        texts: list[str],
        *,
        text_type: str,
    ) -> EmbeddingResult:
        ...


@dataclass(slots=True)
class SemanticIndexBuildResult:
    index_name: str
    index_dir: Path
    entries_indexed: int
    dimensions: int
    model: str
    elapsed_ms: int
    total_tokens: int
    skipped_reason: str | None = None


@dataclass(slots=True)
class SemanticIndexRefreshResult:
    index_name: str
    index_dir: Path
    entries_removed: int
    entries_refreshed: int
    entries_total: int
    total_tokens: int
    skipped_reason: str | None = None


@dataclass(slots=True)
class SemanticHit:
    entry_id: str
    score: float
    rank: int


@dataclass(slots=True)
class _LoadedSemanticIndex:
    metadata: list[dict[str, Any]]
    vectors: array
    dimensions: int
    entries_count: int


def semantic_index_root() -> Path:
    return Path(get_settings().marginalia_home).expanduser() / "semantic-index"


def semantic_index_dir(index_name: str = DEFAULT_INDEX_NAME) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in index_name)
    return semantic_index_root() / (safe or DEFAULT_INDEX_NAME)


def semantic_recall_configured() -> bool:
    settings = get_settings()
    return bool(settings.semantic_recall_enabled and settings.embedding_api_key)


def semantic_index_status(index_name: str = DEFAULT_INDEX_NAME) -> dict[str, Any]:
    settings = get_settings()
    idx_dir = semantic_index_dir(index_name)
    manifest_path = idx_dir / "manifest.json"
    manifest = _read_manifest(manifest_path)
    exists = _semantic_index_exists(index_name)
    compatible = bool(manifest and _manifest_matches_settings(manifest, settings))
    return {
        "index_name": index_name,
        "index_dir": str(idx_dir),
        "exists": exists,
        "provider": manifest.get("provider") if manifest else None,
        "model": manifest.get("model") if manifest else None,
        "dimensions": manifest.get("dimensions") if manifest else None,
        "entries": manifest.get("entries") if manifest else 0,
        "configured_provider": settings.embedding_provider,
        "configured_model": settings.embedding_model,
        "configured_dimensions": settings.embedding_dimensions,
        "compatible": compatible,
        "needs_rebuild": exists and not compatible,
    }


def sqlite_vec_available() -> bool:
    return importlib.util.find_spec("sqlite_vec") is not None


# Serializes all writers to a given semantic index within one event loop. The
# task runner drives multiple ingest/refresh/rebuild tasks concurrently
# (runner.py), so an unsynchronized read-modify-write of entries.jsonl /
# vectors.f32 would lose updates and interleave rows against vector offsets.
# Locks are keyed per running loop so a lock created in one loop is never awaited
# from another (e.g. across tests that each spin up a fresh event loop).
_index_write_locks: "weakref.WeakKeyDictionary[Any, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _index_write_lock(index_name: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    per_loop = _index_write_locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _index_write_locks[loop] = per_loop
    lock = per_loop.get(index_name)
    if lock is None:
        lock = asyncio.Lock()
        per_loop[index_name] = lock
    return lock


# Name of the cross-process lock file kept inside each index directory.
_INDEX_LOCK_FILENAME = ".write.lock"
# The asyncio lock above only serializes writers within a single event loop.
# The documented topology runs the API (uvicorn) and marginalia-worker as
# separate processes (worker.py), so a read-modify-write of entries.jsonl /
# vectors.f32 must also be serialized across processes or last-writer-wins
# os.replace loses updates. Held for the whole critical section. flock/msvcrt
# locks are released automatically when the fd is closed or the process dies,
# so a crashed holder never leaks the lock.
_INDEX_LOCK_TIMEOUT_SECONDS = 300.0


def _acquire_index_lock(fd: int, timeout: float) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "timed out acquiring the semantic index write lock after "
                    f"{timeout:.0f}s; another process is still building the index"
                ) from None
            time.sleep(0.05)


def _release_index_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.asynccontextmanager
async def _index_file_lock(
    index_name: str,
    *,
    timeout: float = _INDEX_LOCK_TIMEOUT_SECONDS,
) -> AsyncIterator[None]:
    lock_dir = semantic_index_dir(index_name)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / _INDEX_LOCK_FILENAME
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            # msvcrt.locking needs at least one byte to lock at offset 0.
            os.write(fd, b"\0")
        await asyncio.to_thread(_acquire_index_lock, fd, timeout)
        try:
            yield
        finally:
            await asyncio.to_thread(_release_index_lock, fd)
    finally:
        os.close(fd)


async def build_semantic_index(
    session: AsyncSession,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    entry_ids: Iterable[str] | None = None,
    batch_size: int | None = None,
    concurrency: int = 1,
    resume: bool = False,
    client: EmbeddingClient | None = None,
    progress_every: int = 50,
) -> SemanticIndexBuildResult:
    if entry_ids is not None:
        # Materialize once (callers may pass a generator) so the emptiness
        # check and the downstream scan see the same sequence.
        entry_ids = list(entry_ids)
        if not entry_ids:
            # An explicit empty selection is a no-op, NOT a full scan. The
            # historical "[] means full library" behavior would silently wipe a
            # populated index (e.g. the eval importer can pass an empty doc
            # map). Return a skip without touching the on-disk index.
            return SemanticIndexBuildResult(
                index_name=index_name,
                index_dir=semantic_index_dir(index_name),
                entries_indexed=0,
                dimensions=0,
                model=get_settings().embedding_model,
                elapsed_ms=0,
                total_tokens=0,
                skipped_reason="empty_entry_ids",
            )
    async with _index_write_lock(index_name):
        async with _index_file_lock(index_name):
            return await _build_semantic_index(
                session,
                index_name=index_name,
                entry_ids=entry_ids,
                batch_size=batch_size,
                concurrency=concurrency,
                resume=resume,
                client=client,
                progress_every=progress_every,
            )


async def _build_semantic_index(
    session: AsyncSession,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    entry_ids: Iterable[str] | None = None,
    batch_size: int | None = None,
    concurrency: int = 1,
    resume: bool = False,
    client: EmbeddingClient | None = None,
    progress_every: int = 50,
) -> SemanticIndexBuildResult:
    settings = get_settings()
    client = client or get_embedding_client(settings)
    batch_size = max(1, int(batch_size or settings.embedding_batch_size or 10))
    concurrency = max(1, int(concurrency or 1))
    started = time.monotonic()
    pairs = await _load_indexable_entries(
        session, list(entry_ids) if entry_ids is not None else None,
    )
    out_dir = semantic_index_dir(index_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_tokens = 0
    count, dimensions, done_ids = _resume_state(
        out_dir / "entries.jsonl.tmp",
        out_dir / "vectors.f32.tmp",
        requested_ids=[entry.id for entry, _file in pairs],
        resume=resume,
    )
    if resume:
        # Fixed tmp names so an interrupted --resume run can be picked up by
        # the next one. Resume is an explicit CLI operation; the concurrent
        # task paths always build with resume=False.
        tmp_meta = out_dir / "entries.jsonl.tmp"
        tmp_vec = out_dir / "vectors.f32.tmp"
    else:
        # Unique per-process tmp names: the asyncio lock only serializes one
        # loop, so another process must not truncate this writer's tmp files.
        tmp_meta = out_dir / f"entries.jsonl.{os.getpid()}.tmp"
        tmp_vec = out_dir / f"vectors.f32.{os.getpid()}.tmp"
    model = settings.embedding_model
    pending_pairs = [
        (entry, file_row)
        for entry, file_row in pairs
        if entry.id not in done_ids
    ]

    if resume and count:
        print(f"  resuming semantic index with {count}/{len(pairs)} entries")

    # Append only when _resume_state actually accepted the tmp files; a
    # rejected state (empty done_ids) must be truncated, not appended to.
    mode = "ab" if done_ids else "wb"
    text_mode = "a" if done_ids else "w"
    # Per-PID tmp name regardless of resume: the manifest is written fresh at
    # the end of every build and is never part of resume state.
    manifest_tmp = out_dir / f"manifest.json.{os.getpid()}.tmp"
    try:
        with tmp_meta.open(text_mode, encoding="utf-8") as meta_f, tmp_vec.open(mode) as vec_f:
            batches = [
                pending_pairs[start:start + batch_size]
                for start in range(0, len(pending_pairs), batch_size)
            ]
            for batch_group_start in range(0, len(batches), concurrency):
                batch_group = batches[batch_group_start:batch_group_start + concurrency]
                tasks = [
                    _embed_batch(client, batch)
                    for batch in batch_group
                ]
                for batch, texts, result in await asyncio.gather(*tasks):
                    total_tokens += result.total_tokens
                    if len(result.vectors) != len(batch):
                        raise RuntimeError(
                            "embedding response count mismatch: "
                            f"expected {len(batch)}, got {len(result.vectors)}"
                        )
                    for (entry, file_row), text, vector in zip(batch, texts, result.vectors):
                        if not vector:
                            continue
                        if dimensions == 0:
                            dimensions = len(vector)
                        if len(vector) != dimensions:
                            raise RuntimeError(
                                f"embedding dimension changed from {dimensions} to {len(vector)}"
                            )
                        vector = _normalize(vector)
                        vec_f.write(struct.pack(f"<{dimensions}f", *vector))
                        meta_f.write(json.dumps({
                            "entry_id": entry.id,
                            "file_id": file_row.id,
                            "display_name": entry.display_name,
                            "text_hash": sha256(text.encode("utf-8")).hexdigest(),
                            "updated_at": str(max(entry.updated_at, file_row.updated_at)),
                        }, ensure_ascii=False) + "\n")
                        count += 1
                    meta_f.flush()
                    vec_f.flush()
                    if progress_every and count and (
                        count % progress_every == 0 or count >= len(pairs)
                    ):
                        print(f"  embedded {count}/{len(pairs)} entries")

        if count <= 0:
            # Zero indexable entries: do NOT publish a dimensions=0 manifest.
            # refresh_semantic_index_for_file would otherwise reject every later
            # refresh as index_config_mismatch forever (a permanent silent
            # lockout). Restore the index-does-not-exist state instead.
            _cleanup_stale_tmps(out_dir)
            _remove_file_index(out_dir)
            _remove_sqlite_vec_index(index_name)
            _load_semantic_index_cached.cache_clear()
            return SemanticIndexBuildResult(
                index_name=index_name,
                index_dir=out_dir,
                entries_indexed=0,
                dimensions=dimensions,
                model=model,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                total_tokens=total_tokens,
                skipped_reason="no_indexable_entries",
            )

        manifest = {
            "version": INDEX_VERSION,
            "index_name": index_name,
            "provider": settings.embedding_provider,
            "model": model,
            "dimensions": dimensions,
            "entries": count,
            "created_at_ms": int(time.time() * 1000),
        }
        manifest_tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_meta.replace(out_dir / "entries.jsonl")
        tmp_vec.replace(out_dir / "vectors.f32")
        manifest_tmp.replace(out_dir / "manifest.json")
    except BaseException:
        # Failed build: unlink the per-PID tmp files so they don't leak. The
        # fixed-name resume tmps (resume=True) are left in place so an explicit
        # --resume can pick them up on the next run.
        with contextlib.suppress(OSError):
            manifest_tmp.unlink()
        if not resume:
            with contextlib.suppress(OSError):
                tmp_meta.unlink()
            with contextlib.suppress(OSError):
                tmp_vec.unlink()
        raise

    _load_semantic_index_cached.cache_clear()
    # Successful build: drop any stale tmp siblings left by earlier interrupted
    # builds (fixed-name resume tmps and *.tmp from prior PIDs). Safe because
    # the cross-process file lock is held for the whole build, so no concurrent
    # writer has live tmp files in this dir.
    _cleanup_stale_tmps(out_dir)

    if _should_build_sqlite_vec_index(settings):
        try:
            _write_sqlite_vec_index(out_dir, dimensions=dimensions, entries_count=count)
        except Exception:
            if settings.semantic_index_backend == "sqlite-vec":
                raise
            # Drop any stale sqlite-vec snapshot so auto-backend searches use
            # the fresh file index instead of preferring outdated vectors.
            _remove_sqlite_vec_index(index_name)
            print(
                "  sqlite-vec index build skipped; falling back to file index",
                file=sys.stderr,
            )

    return SemanticIndexBuildResult(
        index_name=index_name,
        index_dir=out_dir,
        entries_indexed=count,
        dimensions=dimensions,
        model=model,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        total_tokens=total_tokens,
    )


async def refresh_semantic_index_for_file(
    session: AsyncSession,
    file_id: str,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    client: EmbeddingClient | None = None,
) -> SemanticIndexRefreshResult:
    async with _index_write_lock(index_name):
        async with _index_file_lock(index_name):
            return await _refresh_semantic_index_for_file(
                session,
                file_id,
                index_name=index_name,
                client=client,
            )


async def _refresh_semantic_index_for_file(
    session: AsyncSession,
    file_id: str,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    client: EmbeddingClient | None = None,
) -> SemanticIndexRefreshResult:
    settings = get_settings()
    out_dir = semantic_index_dir(index_name)
    if not semantic_recall_configured():
        return SemanticIndexRefreshResult(
            index_name=index_name,
            index_dir=out_dir,
            entries_removed=0,
            entries_refreshed=0,
            entries_total=0,
            total_tokens=0,
            skipped_reason="semantic_recall_not_configured",
        )

    entry_ids = await files_repo.list_live_entry_ids_for_file(session, file_id)
    # Empty entry_ids must stay a removal-only refresh: never hand [] to
    # _load_indexable_entries, whose full-scan fallback would re-embed the
    # whole library after a mid-ingest soft-delete.
    pairs = await _load_indexable_entries(session, entry_ids) if entry_ids else []

    if not _semantic_index_exists(index_name):
        if not pairs:
            return SemanticIndexRefreshResult(
                index_name=index_name,
                index_dir=out_dir,
                entries_removed=0,
                entries_refreshed=0,
                entries_total=0,
                total_tokens=0,
                skipped_reason="no_indexable_entries",
            )
        # Building an index restricted to this one file would look complete
        # and pin every later refresh to the incremental path, leaving the
        # rest of the library unembedded. Schedule a full rebuild instead
        # (deduped so concurrent ingests only enqueue one task).
        await _enqueue_full_rebuild(session, index_name)
        return SemanticIndexRefreshResult(
            index_name=index_name,
            index_dir=out_dir,
            entries_removed=0,
            entries_refreshed=0,
            entries_total=0,
            total_tokens=0,
            skipped_reason="full_rebuild_enqueued",
        )

    manifest_path = out_dir / "manifest.json"
    entries_path = out_dir / "entries.jsonl"
    vectors_path = out_dir / "vectors.f32"
    manifest = _read_manifest(manifest_path)
    if manifest is not None and int(manifest.get("dimensions") or 0) <= 0:
        # A legacy zero-entry index (dimensions=0 manifest, written by builds
        # before the zero-entry removal fix) can never be refreshed in place:
        # it holds no vectors, and when embedding_dimensions is pinned it is
        # rejected as index_config_mismatch forever (a permanent silent
        # lockout). Treat it as missing — drop the stale files and rebuild.
        _remove_file_index(out_dir)
        _remove_sqlite_vec_index(index_name)
        _load_semantic_index_cached.cache_clear()
        if not pairs:
            return SemanticIndexRefreshResult(
                index_name=index_name,
                index_dir=out_dir,
                entries_removed=0,
                entries_refreshed=0,
                entries_total=0,
                total_tokens=0,
                skipped_reason="no_indexable_entries",
            )
        await _enqueue_full_rebuild(session, index_name)
        return SemanticIndexRefreshResult(
            index_name=index_name,
            index_dir=out_dir,
            entries_removed=0,
            entries_refreshed=0,
            entries_total=0,
            total_tokens=0,
            skipped_reason="full_rebuild_enqueued",
        )
    if not manifest or not _manifest_matches_settings(manifest, settings):
        return SemanticIndexRefreshResult(
            index_name=index_name,
            index_dir=out_dir,
            entries_removed=0,
            entries_refreshed=0,
            entries_total=int((manifest or {}).get("entries") or 0),
            total_tokens=0,
            skipped_reason="index_config_mismatch",
        )

    dimensions = int(manifest.get("dimensions") or 0)
    entries_count = int(manifest.get("entries") or 0)
    if not (entries_path.exists() and vectors_path.exists()):
        # Manifest is valid (dimensions > 0) but the vector/metadata files are
        # gone: rebuild from scratch. _build_semantic_index (not the public
        # wrapper) — the write lock is already held and asyncio locks are not
        # reentrant.
        built = await _build_semantic_index(
            session,
            index_name=index_name,
            client=client,
            progress_every=0,
        )
        return SemanticIndexRefreshResult(
            index_name=index_name,
            index_dir=built.index_dir,
            entries_removed=0,
            entries_refreshed=len(pairs),
            entries_total=built.entries_indexed,
            total_tokens=built.total_tokens,
        )

    metadata = _read_metadata(entries_path)
    raw_vectors = vectors_path.read_bytes()
    vector_bytes = dimensions * 4
    available = min(entries_count, len(metadata), len(raw_vectors) // vector_bytes)
    target_entry_ids = {entry.id for entry, _file in pairs} | set(entry_ids)

    kept_metadata: list[dict[str, Any]] = []
    kept_vectors = bytearray()
    removed = 0
    for idx, row in enumerate(metadata[:available]):
        row_file_id = str(row.get("file_id") or "")
        row_entry_id = str(row.get("entry_id") or "")
        if row_file_id == file_id or row_entry_id in target_entry_ids:
            removed += 1
            continue
        kept_metadata.append(row)
        start = idx * vector_bytes
        kept_vectors.extend(raw_vectors[start:start + vector_bytes])

    refreshed_metadata: list[dict[str, Any]] = []
    refreshed_vectors = bytearray()
    total_tokens = 0
    client = client or get_embedding_client(settings)
    batch_size = max(1, int(settings.embedding_batch_size or 10))
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]
        embedded_batch, texts, result = await _embed_batch(client, batch)
        total_tokens += result.total_tokens
        if len(result.vectors) != len(embedded_batch):
            raise RuntimeError(
                "embedding response count mismatch: "
                f"expected {len(embedded_batch)}, got {len(result.vectors)}"
            )
        for (entry, file_row), text, vector in zip(embedded_batch, texts, result.vectors):
            if not vector:
                continue
            if len(vector) != dimensions:
                raise RuntimeError(
                    f"embedding dimension changed from {dimensions} to {len(vector)}"
                )
            vector = _normalize(vector)
            refreshed_vectors.extend(struct.pack(f"<{dimensions}f", *vector))
            refreshed_metadata.append({
                "entry_id": entry.id,
                "file_id": file_row.id,
                "display_name": entry.display_name,
                "text_hash": sha256(text.encode("utf-8")).hexdigest(),
                "updated_at": str(max(entry.updated_at, file_row.updated_at)),
            })

    next_metadata = kept_metadata + refreshed_metadata
    next_vectors = kept_vectors + refreshed_vectors
    next_manifest = {
        **manifest,
        "version": INDEX_VERSION,
        "index_name": index_name,
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dimensions": dimensions,
        "entries": len(next_metadata),
        "created_at_ms": int(time.time() * 1000),
    }
    _replace_file_index(
        out_dir,
        manifest=next_manifest,
        metadata=next_metadata,
        vectors=bytes(next_vectors),
    )
    _load_semantic_index_cached.cache_clear()

    if len(next_metadata) <= 0:
        _remove_sqlite_vec_index(index_name)
    elif _should_build_sqlite_vec_index(settings):
        try:
            _write_sqlite_vec_index(
                out_dir,
                dimensions=dimensions,
                entries_count=len(next_metadata),
            )
        except Exception:
            if settings.semantic_index_backend == "sqlite-vec":
                raise
            # Drop any stale sqlite-vec snapshot so auto-backend searches use
            # the fresh file index instead of preferring outdated vectors.
            _remove_sqlite_vec_index(index_name)
            print(
                "  sqlite-vec index refresh skipped; falling back to file index",
                file=sys.stderr,
            )

    return SemanticIndexRefreshResult(
        index_name=index_name,
        index_dir=out_dir,
        entries_removed=removed,
        entries_refreshed=len(refreshed_metadata),
        entries_total=len(next_metadata),
        total_tokens=total_tokens,
    )


async def _embed_batch(
    client: EmbeddingClient,
    batch: list[tuple[FileEntry, File]],
) -> tuple[list[tuple[FileEntry, File]], list[str], EmbeddingResult]:
    texts = [_entry_text(entry, file_row) for entry, file_row in batch]
    result = await client.embed(texts, text_type="document")
    return batch, texts, result


def _resume_state(
    meta_path: Path,
    vec_path: Path,
    *,
    requested_ids: list[str],
    resume: bool,
) -> tuple[int, int, set[str]]:
    if not resume or not meta_path.exists() or not vec_path.exists():
        return 0, 0, set()
    requested = set(requested_ids)
    done_ids: set[str] = set()
    total_rows = 0
    for row in _read_metadata(meta_path):
        total_rows += 1
        entry_id = str(row.get("entry_id") or "")
        if entry_id in requested:
            done_ids.add(entry_id)
    # Rows outside the requested set (or duplicates) mean the vector file
    # cannot be aligned with done_ids: offsets shift and the dimension
    # inferred below would be wrong.
    if not done_ids or total_rows != len(done_ids):
        return 0, 0, set()
    vector_bytes = vec_path.stat().st_size
    if vector_bytes % (4 * len(done_ids)) != 0:
        return 0, 0, set()
    dimensions = vector_bytes // (4 * len(done_ids))
    if dimensions <= 0:
        return 0, 0, set()
    return len(done_ids), dimensions, done_ids


async def search_semantic_index(
    query: str,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    limit: int = 100,
    client: EmbeddingClient | None = None,
) -> list[SemanticHit]:
    hits = await search_semantic_index_many(
        [query],
        index_name=index_name,
        limit=limit,
        client=client,
    )
    return hits[0] if hits else []


async def search_semantic_index_many(
    queries: list[str],
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    limit: int = 100,
    batch_size: int | None = None,
    client: EmbeddingClient | None = None,
) -> list[list[SemanticHit]]:
    clean = [str(query or "").strip() for query in queries]
    if not clean:
        return []
    if not _semantic_index_exists(index_name):
        return [[] for _query in clean]
    settings = get_settings()
    manifest = _read_manifest(semantic_index_dir(index_name) / "manifest.json")
    if not manifest or not _manifest_matches_settings(manifest, settings):
        return [[] for _query in clean]
    if client is None and not settings.embedding_api_key:
        return [[] for _query in clean]
    batch_size = max(1, int(batch_size or settings.embedding_batch_size or 10))
    client = client or get_embedding_client(settings)
    query_vectors = await _embed_queries_cached(
        client,
        clean,
        index_name=index_name,
        batch_size=batch_size,
    )

    if _should_search_sqlite_vec_index(settings, index_name):
        try:
            return _search_sqlite_vec_index(
                query_vectors,
                index_name=index_name,
                limit=max(1, limit),
            )
        except Exception:
            if settings.semantic_index_backend == "sqlite-vec":
                raise

    loaded = _load_semantic_index(index_name)
    if loaded is None:
        return [[] for _query in clean]
    return [
        _semantic_hits_from_scores(
            loaded.metadata,
            _score_loaded_vectors(
                loaded.vectors,
                qvec,
                dimensions=loaded.dimensions,
                entries_count=loaded.entries_count,
            ),
            limit=max(1, limit),
        )
        for qvec in query_vectors
    ]


async def semantic_entry_rows(
    session: AsyncSession,
    query: str,
    *,
    index_name: str = DEFAULT_INDEX_NAME,
    limit: int = 100,
    client: EmbeddingClient | None = None,
) -> list[dict[str, Any]]:
    hits = await search_semantic_index(
        query,
        index_name=index_name,
        limit=limit,
        client=client,
    )
    if not hits:
        return []
    ids = [hit.entry_id for hit in hits]
    rows = await entries_repo.list_live_with_file_by_ids(session, ids)
    by_id = {entry.id: (entry, file_row) for entry, file_row in rows}
    out: list[dict[str, Any]] = []
    for hit in hits:
        pair = by_id.get(hit.entry_id)
        if pair is None:
            continue
        entry, file_row = pair
        out.append({
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "kind": file_row.kind,
            "summary": file_row.summary,
            "catalog_id": entry.catalog_id,
            "folder_id": entry.folder_id,
            "semantic_score": hit.score,
            "semantic_rank": hit.rank,
        })
    return out


async def _embed_queries_cached(
    client: EmbeddingClient,
    queries: list[str],
    *,
    index_name: str,
    batch_size: int,
) -> list[list[float]]:
    settings = get_settings()
    cache_path = semantic_index_dir(index_name) / "query_cache.jsonl"
    cache = _read_query_cache(cache_path)
    keys = [
        _query_cache_key(
            query,
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
        for query in queries
    ]
    vectors: list[list[float] | None] = [cache.get(key) for key in keys]
    missing_positions = [idx for idx, vector in enumerate(vectors) if vector is None]
    if not missing_positions:
        return [vector or [] for vector in vectors]

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("a", encoding="utf-8") as f:
        for start in range(0, len(missing_positions), batch_size):
            positions = missing_positions[start:start + batch_size]
            batch = [queries[pos] for pos in positions]
            result = await client.embed(batch, text_type="query")
            if len(result.vectors) != len(batch):
                raise RuntimeError(
                    "query embedding response count mismatch: "
                    f"expected {len(batch)}, got {len(result.vectors)}"
                )
            for pos, vector in zip(positions, result.vectors):
                if not vector:
                    # Provider returned no vector: score as a miss but do not
                    # poison query_cache.jsonl with an empty vector forever.
                    vectors[pos] = []
                    continue
                vector = _normalize(vector)
                key = keys[pos]
                vectors[pos] = vector
                f.write(json.dumps({
                    "key": key,
                    "provider": settings.embedding_provider,
                    "model": settings.embedding_model,
                    "dimensions": settings.embedding_dimensions,
                    "text_type": "query",
                    "text_hash": sha256(queries[pos].encode("utf-8")).hexdigest(),
                    "vector": vector,
                }, ensure_ascii=False) + "\n")
            f.flush()
    return [vector or [] for vector in vectors]


def _read_query_cache(path: Path) -> dict[str, list[float]]:
    if not path.exists():
        return {}
    out: dict[str, list[float]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = str(row.get("key") or "")
            vector = row.get("vector")
            # Ignore zero-length vectors written before empty embeddings were
            # rejected; treating them as misses lets them be re-embedded.
            if key and isinstance(vector, list) and vector:
                out[key] = [float(v) for v in vector]
    return out


def _query_cache_key(
    query: str,
    *,
    provider: str,
    model: str,
    dimensions: int,
) -> str:
    raw = f"{provider}\0{model}\0{dimensions}\0query\0{query}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _semantic_index_exists(index_name: str) -> bool:
    idx_dir = semantic_index_dir(index_name)
    manifest_path = idx_dir / "manifest.json"
    file_paths_exist = (
        (idx_dir / "entries.jsonl").exists()
        and (idx_dir / "vectors.f32").exists()
    )
    return manifest_path.exists() and (
        file_paths_exist or _sqlite_vec_index_path(index_name).exists()
    )


def _read_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _manifest_matches_settings(manifest: dict[str, Any], settings: Any) -> bool:
    if int(manifest.get("version") or 0) != INDEX_VERSION:
        return False
    provider = manifest.get("provider")
    if provider is not None and str(provider) != settings.embedding_provider:
        return False
    if str(manifest.get("model") or "") != settings.embedding_model:
        return False
    try:
        dimensions = int(manifest.get("dimensions") or 0)
    except (TypeError, ValueError):
        return False
    return dimensions == int(settings.embedding_dimensions or 0)


def _replace_file_index(
    index_dir: Path,
    *,
    manifest: dict[str, Any],
    metadata: list[dict[str, Any]],
    vectors: bytes,
) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    # Unique per-process tmp names so a concurrent process cannot truncate
    # this writer's tmp files (the asyncio write lock only covers one loop).
    meta_tmp = index_dir / f"entries.jsonl.{os.getpid()}.tmp"
    vec_tmp = index_dir / f"vectors.f32.{os.getpid()}.tmp"
    manifest_tmp = index_dir / f"manifest.json.{os.getpid()}.tmp"
    with meta_tmp.open("w", encoding="utf-8") as f:
        for row in metadata:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    vec_tmp.write_bytes(vectors)
    manifest_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    meta_tmp.replace(index_dir / "entries.jsonl")
    vec_tmp.replace(index_dir / "vectors.f32")
    manifest_tmp.replace(index_dir / "manifest.json")


def _sqlite_vec_index_path(index_name: str = DEFAULT_INDEX_NAME) -> Path:
    return semantic_index_dir(index_name) / SQLITE_VEC_INDEX_FILENAME


def _remove_sqlite_vec_index(index_name: str = DEFAULT_INDEX_NAME) -> None:
    path = _sqlite_vec_index_path(index_name)
    if path.exists():
        path.unlink()


def _remove_file_index(index_dir: Path) -> None:
    """Restore the index-does-not-exist state for the file-index backend.

    Used when a build produced zero vectors: leaving a dimensions=0 manifest
    behind would make every later refresh reject it as index_config_mismatch.
    """
    for name in ("entries.jsonl", "vectors.f32", "manifest.json"):
        with contextlib.suppress(OSError):
            (index_dir / name).unlink()


def _cleanup_stale_tmps(index_dir: Path) -> None:
    """Delete leftover build tmp files (fixed-name and per-PID) in the dir.

    Called only on a successful build while the cross-process write lock is
    held, so no other writer has live tmp files here.
    """
    patterns = (
        "entries.jsonl.tmp",
        "vectors.f32.tmp",
        "manifest.json.tmp",
        "entries.jsonl.*.tmp",
        "vectors.f32.*.tmp",
        "manifest.json.*.tmp",
    )
    for pattern in patterns:
        for path in index_dir.glob(pattern):
            with contextlib.suppress(OSError):
                path.unlink()


async def _enqueue_full_rebuild(session: AsyncSession, index_name: str) -> None:
    """Enqueue a deduped full semantic-index rebuild task.

    Local import: marginalia.tasks pulls in the handlers package, which imports
    this module.
    """
    from marginalia.tasks.enqueue import enqueue
    from marginalia.tasks.kinds import KIND_REBUILD_SEMANTIC_INDEX

    await enqueue(
        session,
        kind=KIND_REBUILD_SEMANTIC_INDEX,
        payload={"index_name": index_name, "concurrency": 1},
        dedup_key=f"{KIND_REBUILD_SEMANTIC_INDEX}:{index_name}",
        max_attempts=2,
    )


def _should_build_sqlite_vec_index(settings: Any) -> bool:
    if settings.semantic_index_backend == "file":
        return False
    if settings.semantic_index_backend == "sqlite-vec":
        return True
    return sqlite_vec_available()


def _should_search_sqlite_vec_index(settings: Any, index_name: str) -> bool:
    if settings.semantic_index_backend == "file":
        return False
    path = _sqlite_vec_index_path(index_name)
    if not path.exists():
        return False
    if settings.semantic_index_backend == "sqlite-vec":
        return True
    return sqlite_vec_available()


def _connect_sqlite_vec(path: Path) -> sqlite3.Connection:
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "sqlite-vec is not installed; install marginalia[semantic] or set "
            "SEMANTIC_INDEX_BACKEND=file"
        ) from exc

    conn = sqlite3.connect(str(path))
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        conn.close()
        raise
    finally:
        try:
            conn.enable_load_extension(False)
        except sqlite3.Error:
            pass
    return conn


def _write_sqlite_vec_index(
    index_dir: Path,
    *,
    dimensions: int,
    entries_count: int,
) -> None:
    if dimensions <= 0 or entries_count <= 0:
        return
    manifest_path = index_dir / "manifest.json"
    entries_path = index_dir / "entries.jsonl"
    vectors_path = index_dir / "vectors.f32"
    if not (manifest_path.exists() and entries_path.exists() and vectors_path.exists()):
        return

    metadata = _read_metadata(entries_path)
    raw_vectors = vectors_path.read_bytes()
    vector_bytes = dimensions * 4
    available = min(entries_count, len(metadata), len(raw_vectors) // vector_bytes)
    if available <= 0:
        return

    db_path = index_dir / SQLITE_VEC_INDEX_FILENAME
    tmp_path = index_dir / f"{SQLITE_VEC_INDEX_FILENAME}.{os.getpid()}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    conn = _connect_sqlite_vec(tmp_path)
    try:
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("""
            CREATE TABLE semantic_entries (
                rowid INTEGER PRIMARY KEY,
                entry_id TEXT NOT NULL UNIQUE,
                file_id TEXT,
                display_name TEXT,
                text_hash TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE semantic_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_entries USING vec0(embedding float[{dimensions}])"
        )
        conn.executemany(
            """
            INSERT INTO semantic_index_meta(key, value)
            VALUES (?, ?)
            """,
            [
                ("version", str(INDEX_VERSION)),
                ("dimensions", str(dimensions)),
                ("entries", str(available)),
                ("source_manifest", manifest_path.read_text(encoding="utf-8")),
            ],
        )
        entry_rows: list[tuple[int, str, str, str, str, str]] = []
        vector_rows: list[tuple[int, sqlite3.Binary]] = []
        for idx, row in enumerate(metadata[:available]):
            rowid = idx + 1
            entry_rows.append((
                rowid,
                str(row.get("entry_id") or ""),
                str(row.get("file_id") or ""),
                str(row.get("display_name") or ""),
                str(row.get("text_hash") or ""),
                str(row.get("updated_at") or ""),
            ))
            start = idx * vector_bytes
            vector_rows.append((
                rowid,
                sqlite3.Binary(raw_vectors[start:start + vector_bytes]),
            ))
        conn.executemany(
            """
            INSERT INTO semantic_entries(
                rowid, entry_id, file_id, display_name, text_hash, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            entry_rows,
        )
        conn.executemany(
            "INSERT INTO vec_entries(rowid, embedding) VALUES (?, ?)",
            vector_rows,
        )
        conn.commit()
    except Exception:
        conn.close()
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    else:
        conn.close()
        tmp_path.replace(db_path)


def _search_sqlite_vec_index(
    query_vectors: list[list[float]],
    *,
    index_name: str,
    limit: int,
) -> list[list[SemanticHit]]:
    idx_dir = semantic_index_dir(index_name)
    manifest = json.loads((idx_dir / "manifest.json").read_text(encoding="utf-8"))
    dimensions = int(manifest.get("dimensions") or 0)
    if dimensions <= 0:
        return [[] for _query in query_vectors]
    conn = _connect_sqlite_vec(_sqlite_vec_index_path(index_name))
    try:
        out: list[list[SemanticHit]] = []
        for qvec in query_vectors:
            if len(qvec) != dimensions:
                out.append([])
                continue
            blob = sqlite3.Binary(struct.pack(f"<{dimensions}f", *qvec))
            rows = conn.execute(
                """
                SELECT semantic_entries.entry_id, vec_entries.distance
                FROM vec_entries
                JOIN semantic_entries ON semantic_entries.rowid = vec_entries.rowid
                WHERE embedding MATCH ? AND k = ?
                ORDER BY vec_entries.distance
                """,
                (blob, limit),
            ).fetchall()
            hits = [
                SemanticHit(
                    entry_id=str(entry_id),
                    score=1.0 / (1.0 + float(distance or 0.0)),
                    rank=rank,
                )
                for rank, (entry_id, distance) in enumerate(rows, start=1)
            ]
            out.append(hits)
        return out
    finally:
        conn.close()


def _load_semantic_index(index_name: str = DEFAULT_INDEX_NAME) -> _LoadedSemanticIndex | None:
    idx_dir = semantic_index_dir(index_name)
    manifest_path = idx_dir / "manifest.json"
    entries_path = idx_dir / "entries.jsonl"
    vectors_path = idx_dir / "vectors.f32"
    if not (manifest_path.exists() and entries_path.exists() and vectors_path.exists()):
        return None
    return _load_semantic_index_cached(
        index_name,
        manifest_path.stat().st_mtime_ns,
        entries_path.stat().st_mtime_ns,
        vectors_path.stat().st_mtime_ns,
    )


@lru_cache(maxsize=4)
def _load_semantic_index_cached(
    index_name: str,
    manifest_mtime_ns: int,
    entries_mtime_ns: int,
    vectors_mtime_ns: int,
) -> _LoadedSemanticIndex | None:
    del manifest_mtime_ns, entries_mtime_ns, vectors_mtime_ns
    idx_dir = semantic_index_dir(index_name)
    manifest_path = idx_dir / "manifest.json"
    entries_path = idx_dir / "entries.jsonl"
    vectors_path = idx_dir / "vectors.f32"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dimensions = int(manifest.get("dimensions") or 0)
    entries_count = int(manifest.get("entries") or 0)
    if dimensions <= 0 or entries_count <= 0:
        return None
    metadata = _read_metadata(entries_path)
    vectors = _read_vector_array(vectors_path)
    entries_count = min(entries_count, len(metadata), len(vectors) // dimensions)
    return _LoadedSemanticIndex(
        metadata=metadata,
        vectors=vectors,
        dimensions=dimensions,
        entries_count=entries_count,
    )


def _semantic_hits_from_scores(
    metadata: list[dict[str, Any]],
    scores: list[tuple[int, float]],
    *,
    limit: int,
) -> list[SemanticHit]:
    top = sorted(scores, key=lambda item: item[1], reverse=True)[:limit]
    return [
        SemanticHit(entry_id=str(metadata[idx]["entry_id"]), score=score, rank=rank)
        for rank, (idx, score) in enumerate(top, start=1)
        if idx < len(metadata)
    ]


async def _load_indexable_entries(
    session: AsyncSession,
    entry_ids: list[str] | None,
) -> list[tuple[FileEntry, File]]:
    if entry_ids is not None:
        # [] means "no entries", not "full scan": a refresh whose entries were
        # all soft-deleted must not re-embed the entire library.
        if not entry_ids:
            return []
        rows = await entries_repo.list_live_with_file_by_ids(session, entry_ids)
        by_id = {entry.id: (entry, file_row) for entry, file_row in rows}
        return [
            pair
            for eid in entry_ids
            if (pair := by_id.get(eid)) is not None
            and pair[0].lifecycle in entries_repo.ACTIVE_LIFECYCLES
            and pair[1].ingest_status == "done"
        ]
    stmt = (
        select(FileEntry, File)
        .join(File, File.id == FileEntry.file_id)
        .where(
            FileEntry.deleted_at.is_(None),
            File.deleted_at.is_(None),
            FileEntry.lifecycle.in_(entries_repo.ACTIVE_LIFECYCLES),
            File.ingest_status == "done",
        )
        .order_by(FileEntry.updated_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    return [(entry, file_row) for entry, file_row in rows]


def _entry_text(entry: FileEntry, file_row: File) -> str:
    parts = [
        f"name: {entry.display_name or ''}",
        f"summary: {file_row.summary or ''}",
        _description_text(file_row.description),
        f"file_extra: {file_row.extra or ''}",
        f"entry_extra: {entry.extra or ''}",
    ]
    text = "\n".join(part for part in parts if part.strip())
    return text[:EMBEDDING_TEXT_MAX_CHARS]


def _description_text(description: Any) -> str:
    if isinstance(description, str):
        return description
    if not isinstance(description, dict):
        return ""
    parts: list[str] = []
    text = description.get("text")
    if isinstance(text, str):
        parts.append(f"description: {text}")
    sections = description.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            title = section.get("title")
            summary = section.get("summary")
            key_terms = section.get("key_terms")
            line = " ".join(
                str(item)
                for item in (title, summary, _stringify(key_terms))
                if item
            )
            if line:
                parts.append(f"section: {line}")
    return "\n".join(parts)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify(item) for item in value.values())
    if value is None:
        return ""
    return str(value)


def _read_metadata(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def _score_vectors(
    path: Path,
    qvec: list[float],
    *,
    dimensions: int,
    entries_count: int,
) -> list[tuple[int, float]]:
    return _score_loaded_vectors(
        _read_vector_array(path),
        qvec,
        dimensions=dimensions,
        entries_count=entries_count,
    )


def _read_vector_array(path: Path) -> array:
    data = array("f")
    data.frombytes(path.read_bytes())
    if sys.byteorder != "little":
        data.byteswap()
    return data


def _score_loaded_vectors(
    data: array,
    qvec: list[float],
    *,
    dimensions: int,
    entries_count: int,
) -> list[tuple[int, float]]:
    # An empty/mismatched query vector cannot be scored (math.sumprod requires
    # equal lengths); return no hits like the sqlite-vec path does.
    if dimensions <= 0 or len(qvec) != dimensions:
        return []
    scores: list[tuple[int, float]] = []
    available = min(entries_count, len(data) // dimensions)
    q = array("f", qvec)
    sumprod = getattr(math, "sumprod", None)
    for idx in range(available):
        start = idx * dimensions
        vector = data[start:start + dimensions]
        if sumprod is not None:
            score = sumprod(q, vector)
        else:
            score = sum(qi * vi for qi, vi in zip(q, vector))
        scores.append((idx, float(score)))
    return scores


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector
    return [v / norm for v in vector]
