"""Regression tests for the 2026-07-02 audit fixes touching metadata/journal
search, semantic-index refresh, the settings overlay, the upload cap, and the
text decoder.

Each test is written to FAIL against the pre-fix behavior and PASS now. Bug
numbers refer to headings in audit-report-2026-07-02.md.
"""
from __future__ import annotations

import asyncio
import io
import json
from array import array
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marginalia.db.bootstrap import bootstrap_schema_sync
from marginalia.db.fts import (
    ENTRY_METADATA_FTS_TABLE,
    ENTRY_METADATA_FTS_TRIGGERS,
)
from marginalia.db.models import File, FileEntry
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_entry(session, *, summary: str, name: str = "d.txt", extra: str = "") -> str:
    """Insert a File + active FileEntry, returning the entry id."""
    file_id = new_id()
    entry_id = new_id()
    session.add(File(
        id=file_id,
        storage_key=f"00/{file_id[:2]}/{file_id}",
        sha256="a" * 64,
        size_bytes=10,
        mime_type="text/plain",
        original_ext=".txt",
        kind="text",
        summary=summary,
        description={"sections": []},
        extra=extra,
        ingest_status="done",
        ingested_at=_now(),
        deleted_at=None,
        created_at=_now(),
        updated_at=_now(),
    ))
    session.add(FileEntry(
        id=entry_id,
        folder_id=None,
        file_id=file_id,
        display_name=name,
        lifecycle="active",
        catalog_id=None,
        extra="",
        deleted_at=None,
        purge_after=None,
        created_at=_now(),
        updated_at=_now(),
    ))
    return entry_id


async def _bootstrap(db_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(bootstrap_schema_sync)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _fts_present(session) -> bool:
    row = (
        await session.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = :name"
            ),
            {"name": ENTRY_METADATA_FTS_TABLE},
        )
    ).scalar_one_or_none()
    return bool(row)


# ---------------------------------------------------------------------------
# Bug #29 — short non-CJK terms ("AI"/"ML"/"Go") rescued via LIKE
# ---------------------------------------------------------------------------

def test_short_non_cjk_terms_are_rescued_as_like_terms() -> None:
    from marginalia.repositories.entries import _metadata_short_like_terms

    # Pre-fix this was _metadata_short_cjk_like_terms, which required
    # _contains_cjk(term) and therefore dropped every ASCII short term.
    assert _metadata_short_like_terms(["AI", "ML", "protocol"]) == ["AI", "ML"]


@pytest.mark.asyncio
async def test_entries_search_matches_ai_only_entry_via_like_rescue(
    tmp_path: Path,
) -> None:
    from marginalia.repositories import entries as entries_repo

    engine, factory = await _bootstrap(tmp_path / "fts29.db")
    try:
        async with factory() as session:
            if not await _fts_present(session):
                pytest.skip("SQLite build does not provide FTS5 trigram")
            ai_id = _add_entry(session, summary="Advances in AI.", name="ai.txt")
            proto_id = _add_entry(
                session, summary="The protocol handshake design.", name="p.txt",
            )
            await session.commit()

        # "AI" is sub-trigram (dropped from the FTS query); it must be
        # OR'd back in as a LIKE term so this entry still surfaces even
        # though it lacks the >=3-char term "protocol".
        async with factory() as session:
            rows = await entries_repo.search_filtered(
                session, text=["AI", "protocol"], lifecycle=["active"], limit=10,
            )
            ids = {entry.id for entry, _file in rows}
            assert ai_id in ids
            assert proto_id in ids
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Bug #64 — LIKE/ILIKE wildcards in user terms are escaped (literal match)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_entries_like_fallback_escapes_underscore_wildcard(
    tmp_path: Path,
) -> None:
    from marginalia.repositories import entries as entries_repo

    engine, factory = await _bootstrap(tmp_path / "esc64.db")
    try:
        async with factory() as session:
            literal_id = _add_entry(
                session, summary="See report_2024 attached now.", name="lit.txt",
            )
            wildcard_id = _add_entry(
                session, summary="See reportX2024 attached now.", name="wild.txt",
            )
            await session.commit()

        # Force the pure-LIKE fallback path (used when no FTS index exists)
        # so a >=3-char term still exercises _escape_like_term.
        async with factory() as session:
            for trigger in ENTRY_METADATA_FTS_TRIGGERS:
                await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
            await session.execute(
                text(f"DROP TABLE IF EXISTS {ENTRY_METADATA_FTS_TABLE}")
            )
            await session.commit()

        async with factory() as session:
            rows = await entries_repo.search_filtered(
                session, text=["report_2024"], lifecycle=["active"], limit=10,
            )
            ids = {entry.id for entry, _file in rows}
            # `_` is a single-char wildcard; unescaped it also matches
            # "reportX2024". Escaped, only the literal underscore matches.
            assert literal_id in ids
            assert wildcard_id not in ids
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_journal_search_escapes_percent_wildcard(tmp_path: Path) -> None:
    from marginalia.db.models import Journal
    from marginalia.repositories import journal as journal_repo

    engine, factory = await _bootstrap(tmp_path / "journal64.db")
    try:
        async with factory() as session:
            session.add(Journal(
                id=new_id(),
                conversation_id="c-fake",
                note="score is 100% complete",
                entry_ids=[],
                tags=[],
                source_kind="insight",
                created_at=_now(),
            ))
            session.add(Journal(
                id=new_id(),
                conversation_id="c-fake",
                note="value 1000 units and counting",
                entry_ids=[],
                tags=[],
                source_kind="insight",
                created_at=_now(),
            ))
            await session.commit()

        async with factory() as session:
            rows = await journal_repo.search(
                session,
                cutoff=datetime(2000, 1, 1, tzinfo=timezone.utc),
                kinds=["insight"],
                conversation_id=None,
                include_superseded=True,
                include_invalidated=True,
                text=["100%"],
                order="newest_first",
                limit=10,
            )
            notes = {row.note for row in rows}
            # "%" unescaped is a wildcard → "%100%%" also matches "1000".
            assert "score is 100% complete" in notes
            assert "value 1000 units and counting" not in notes
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Bug #33 — 2-3 char CJK terms survive the short-noise-word filter
# ---------------------------------------------------------------------------

def test_rank_and_score_terms_keep_short_cjk() -> None:
    from marginalia.agent.tools.recall_knowledge import _score_terms
    from marginalia.agent.tools.search_metadata import _rank_terms

    # 2- and 3-char CJK words have no digit/uppercase, so the len<4 filter
    # used to drop them entirely; they must now be kept.
    assert "合同" in _rank_terms(["合同"])
    assert "民法典" in _rank_terms(["民法典"])
    assert "合同" in _score_terms(["合同"])
    assert "民法典" in _score_terms(["民法典"])

    # A short ASCII noise word is still dropped (guard didn't over-widen).
    assert _rank_terms(["ab"]) == []
    assert _score_terms(["ab"]) == []


# ---------------------------------------------------------------------------
# Bugs #30 / #31 — semantic refresh guards
# ---------------------------------------------------------------------------

@dataclass
class _FakeEmbeddingClient:
    async def embed(self, texts: list[str], *, text_type: str):
        from marginalia.semantic.embeddings import EmbeddingResult

        return EmbeddingResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts], total_tokens=len(texts),
        )


def _configure_semantic_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("MARGINALIA_HOME", str(home))
    monkeypatch.setenv("SEMANTIC_RECALL_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_API_KEY", "fake-key")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3")
    monkeypatch.setenv("SEMANTIC_INDEX_BACKEND", "file")
    from marginalia.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_first_refresh_enqueues_full_rebuild_not_single_file_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marginalia.db.models.tasks import Task
    from marginalia.semantic import index as sem
    from marginalia.tasks.kinds import KIND_REBUILD_SEMANTIC_INDEX

    _configure_semantic_env(monkeypatch, tmp_path / "home")
    engine, factory = await _bootstrap(tmp_path / "sem30.db")
    try:
        async with factory() as session:
            file_a = new_id()
            entry_a = new_id()
            session.add(File(
                id=file_a, storage_key="00/aa/a", sha256="a" * 64, size_bytes=10,
                mime_type="text/plain", original_ext=".txt", kind="text",
                summary="Raft consensus leader election.",
                description={"sections": []}, extra="", ingest_status="done",
                ingested_at=_now(), deleted_at=None, created_at=_now(), updated_at=_now(),
            ))
            session.add(FileEntry(
                id=entry_a, folder_id=None, file_id=file_a, display_name="a.txt",
                lifecycle="active", catalog_id=None, extra="", deleted_at=None,
                purge_after=None, created_at=_now(), updated_at=_now(),
            ))
            # A second file exists but is NOT the refresh target; a
            # single-file index would silently omit it.
            file_b = new_id()
            entry_b = new_id()
            session.add(File(
                id=file_b, storage_key="00/bb/b", sha256="b" * 64, size_bytes=10,
                mime_type="text/plain", original_ext=".txt", kind="text",
                summary="Cooking sourdough bread.",
                description={"sections": []}, extra="", ingest_status="done",
                ingested_at=_now(), deleted_at=None, created_at=_now(), updated_at=_now(),
            ))
            session.add(FileEntry(
                id=entry_b, folder_id=None, file_id=file_b, display_name="b.txt",
                lifecycle="active", catalog_id=None, extra="", deleted_at=None,
                purge_after=None, created_at=_now(), updated_at=_now(),
            ))
            await session.commit()

        async with factory() as session:
            result = await sem.refresh_semantic_index_for_file(
                session, file_a, client=_FakeEmbeddingClient(),
            )
            kinds = (await session.execute(select(Task.kind))).scalars().all()

        assert result.skipped_reason == "full_rebuild_enqueued"
        assert result.entries_refreshed == 0
        assert kinds == [KIND_REBUILD_SEMANTIC_INDEX]
        # No library-wide index was materialized from just this one file.
        assert not (sem.semantic_index_dir() / "entries.jsonl").exists()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_load_indexable_entries_empty_list_is_not_full_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marginalia.semantic import index as sem

    _configure_semantic_env(monkeypatch, tmp_path / "home")
    engine, factory = await _bootstrap(tmp_path / "sem31.db")
    try:
        async with factory() as session:
            _add_entry(session, summary="Raft consensus leader election.")
            _add_entry(session, summary="Cooking sourdough bread.")
            await session.commit()

        async with factory() as session:
            # [] means "no live entries" (removal-only refresh), NOT a
            # library-wide re-embed. Pre-fix `if entry_ids:` fell through
            # to the full scan and returned every entry.
            empty = await sem._load_indexable_entries(session, [])
            all_rows = await sem._load_indexable_entries(session, None)

        assert empty == []
        assert len(all_rows) == 2
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Bug #34 — embedding text is truncated to EMBEDDING_TEXT_MAX_CHARS
# ---------------------------------------------------------------------------

def test_entry_text_is_truncated_to_embedding_cap() -> None:
    from marginalia.semantic.index import EMBEDDING_TEXT_MAX_CHARS, _entry_text

    entry = SimpleNamespace(display_name="d.txt", extra="")
    file_row = SimpleNamespace(
        summary="A" * (EMBEDDING_TEXT_MAX_CHARS * 3),
        description=None,
        extra="",
    )
    result = _entry_text(entry, file_row)
    assert len(result) == EMBEDDING_TEXT_MAX_CHARS


# ---------------------------------------------------------------------------
# Bug #63 — zero-length query vectors are neither scored nor cached
# ---------------------------------------------------------------------------

def test_score_loaded_vectors_rejects_empty_or_mismatched_query() -> None:
    from marginalia.semantic.index import _score_loaded_vectors

    data = array("f", [0.0, 1.0, 0.0])
    # Empty query vector: pre-fix math.sumprod raised on unequal lengths.
    assert _score_loaded_vectors(data, [], dimensions=3, entries_count=1) == []
    # Wrong-length query vector is likewise a no-hit.
    assert _score_loaded_vectors(
        data, [1.0, 0.0], dimensions=3, entries_count=1,
    ) == []


def test_read_query_cache_ignores_zero_length_vectors(tmp_path: Path) -> None:
    from marginalia.semantic.index import _read_query_cache

    cache_path = tmp_path / "query_cache.jsonl"
    cache_path.write_text(
        json.dumps({"key": "empty", "vector": []}) + "\n"
        + json.dumps({"key": "good", "vector": [1.0, 2.0, 3.0]}) + "\n",
        encoding="utf-8",
    )
    cache = _read_query_cache(cache_path)
    assert "empty" not in cache
    assert cache["good"] == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_empty_query_embedding_not_written_to_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from marginalia.semantic import index as sem
    from marginalia.semantic.embeddings import EmbeddingResult

    _configure_semantic_env(monkeypatch, tmp_path / "home")

    @dataclass
    class _EmptyClient:
        async def embed(self, texts: list[str], *, text_type: str):
            return EmbeddingResult(vectors=[[] for _ in texts], total_tokens=0)

    index_name = "empty-embed-idx"
    sem.semantic_index_dir(index_name).mkdir(parents=True, exist_ok=True)
    result = await sem._embed_queries_cached(
        _EmptyClient(), ["hello"], index_name=index_name, batch_size=10,
    )
    assert result == [[]]

    cache_path = sem.semantic_index_dir(index_name) / "query_cache.jsonl"
    # Either no cache file, or one with no poisoned empty-vector row.
    if cache_path.exists():
        assert cache_path.read_text(encoding="utf-8").strip() == ""


# ---------------------------------------------------------------------------
# Bug #54 — overlay null-clears pass None through (no 422, no explicit False)
# ---------------------------------------------------------------------------

def test_validate_and_normalize_passes_null_clears_through() -> None:
    from marginalia.services.config_overlay import validate_and_normalize

    # int field: pre-fix int(None) raised OverlayValidationError (422).
    assert validate_and_normalize({"embedding_dimensions": None}) == {
        "embedding_dimensions": None,
    }
    # float field: pre-fix float(None) raised.
    assert validate_and_normalize({"agent_turn_timeout_seconds": None}) == {
        "agent_turn_timeout_seconds": None,
    }
    # bool field: pre-fix bool(None) stored an explicit False override.
    assert validate_and_normalize({"rerank_enabled": None}) == {
        "rerank_enabled": None,
    }
    # A blank string is treated the same as an explicit null.
    assert validate_and_normalize({"embedding_dimensions": ""}) == {
        "embedding_dimensions": None,
    }


# ---------------------------------------------------------------------------
# Bug #27 — upload size cap: 413 + no partial object; default 0 unchanged
# ---------------------------------------------------------------------------

async def _run_upload(home: Path, max_bytes: int | None) -> dict:
    import os

    os.environ["MARGINALIA_HOME"] = str(home)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["WORKER_ENABLED"] = "false"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
    os.environ["LLM_DEFAULT_MODEL"] = "fake-model"
    if max_bytes is None:
        os.environ.pop("MARGINALIA_UPLOAD_MAX_BYTES", None)
    else:
        os.environ["MARGINALIA_UPLOAD_MAX_BYTES"] = str(max_bytes)

    from marginalia.config import get_settings
    from marginalia.db.engine import dispose_engine, get_engine, get_session_factory
    from marginalia.storage import reset_storage_cache

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage_cache()
    await dispose_engine()

    from marginalia.db.models import Base

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    import httpx
    from httpx import ASGITransport

    from marginalia.main import app

    out: dict = {}
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            big = await c.post(
                "/v1/upload",
                params={"remote_path": "/big.bin"},
                files={"file": ("big.bin", io.BytesIO(b"X" * 5000), "application/octet-stream")},
            )
            out["big_status"] = big.status_code
            small = await c.post(
                "/v1/upload",
                params={"remote_path": "/small.bin"},
                files={"file": ("small.bin", io.BytesIO(b"Y" * 100), "application/octet-stream")},
            )
            out["small_status"] = small.status_code

    factory = get_session_factory()
    async with factory() as session:
        out["file_count"] = (
            await session.execute(select(func.count()).select_from(File))
        ).scalar_one()
    objects = home / "objects"
    out["blob_count"] = (
        sum(1 for p in objects.rglob("*") if p.is_file()) if objects.exists() else 0
    )
    await dispose_engine()
    return out


@pytest.mark.asyncio
async def test_upload_cap_rejects_oversized_and_leaves_no_partial(
    tmp_path: Path,
) -> None:
    home = tmp_path / f"cap_{uuid4().hex[:6]}"
    result = await _run_upload(home, max_bytes=1000)
    # 5000-byte body exceeds the streaming cap → 413.
    assert result["big_status"] == 413
    # The under-cap upload still succeeds.
    assert result["small_status"] == 201
    # Only the small file is persisted; the rejected upload left no
    # DB row and no stored blob.
    assert result["file_count"] == 1
    assert result["blob_count"] == 1


@pytest.mark.asyncio
async def test_upload_default_zero_cap_is_unlimited(tmp_path: Path) -> None:
    home = tmp_path / f"nocap_{uuid4().hex[:6]}"
    result = await _run_upload(home, max_bytes=None)
    assert result["big_status"] == 201
    assert result["small_status"] == 201
    assert result["file_count"] == 2


# ---------------------------------------------------------------------------
# Bug #8 — text decode falls back to cp1252/latin-1, not UTF-16 mojibake
# ---------------------------------------------------------------------------

def test_decode_text_uses_cp1252_fallback_not_utf16_mojibake() -> None:
    from marginalia.pipelines.text import _decode_text

    # b"Caf\xe9" is 4 bytes; a bare utf-16 attempt "succeeds" and produces
    # mojibake. The fix only tries utf-16 on a BOM, then cp1252/latin-1.
    assert _decode_text(b"Caf\xe9") == "Café"


def test_decode_text_still_handles_utf16_bom() -> None:
    from marginalia.pipelines.text import _decode_text

    assert _decode_text("Café résumé".encode("utf-16")) == "Café résumé"
