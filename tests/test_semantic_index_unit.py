from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marginalia.db.bootstrap import bootstrap_schema_sync
from marginalia.db.models import File, FileEntry
from marginalia.config import Settings
from marginalia.semantic.embeddings import EmbeddingConfigError, get_embedding_client
from marginalia.semantic.embeddings import EmbeddingResult
from marginalia.semantic.index import (
    SQLITE_VEC_INDEX_FILENAME,
    build_semantic_index,
    search_semantic_index,
    search_semantic_index_many,
    semantic_index_dir,
    sqlite_vec_available,
)
from marginalia.semantic.rerank import _parse_rerank_hits
from marginalia.utils.ids import new_id


@dataclass
class _FakeEmbeddingClient:
    async def embed(self, texts: list[str], *, text_type: str) -> EmbeddingResult:
        vectors = []
        for text in texts:
            haystack = text.casefold()
            if "raft" in haystack or "leader" in haystack:
                vectors.append([1.0, 0.0, 0.0])
            elif "cooking" in haystack or "sourdough" in haystack:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([0.0, 0.0, 1.0])
        return EmbeddingResult(vectors=vectors, total_tokens=len(texts))


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_semantic_index_builds_and_searches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MARGINALIA_HOME", str(tmp_path / "home"))
    if sqlite_vec_available():
        monkeypatch.setenv("SEMANTIC_INDEX_BACKEND", "sqlite-vec")
    from marginalia.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = _now()
    raft_file_id = new_id()
    raft_entry_id = new_id()
    cooking_file_id = new_id()
    cooking_entry_id = new_id()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(bootstrap_schema_sync)

        async with factory() as session:
            session.add(File(
                id=raft_file_id,
                storage_key="00/aa/raft",
                sha256="a" * 64,
                size_bytes=10,
                mime_type="text/plain",
                original_ext=".txt",
                kind="text",
                summary="Raft consensus uses leader election.",
                description={"sections": []},
                extra="",
                ingest_status="done",
                ingested_at=now,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(FileEntry(
                id=raft_entry_id,
                folder_id=None,
                file_id=raft_file_id,
                display_name="raft.txt",
                lifecycle="active",
                catalog_id=None,
                extra="",
                deleted_at=None,
                purge_after=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(File(
                id=cooking_file_id,
                storage_key="00/aa/cooking",
                sha256="b" * 64,
                size_bytes=10,
                mime_type="text/plain",
                original_ext=".txt",
                kind="text",
                summary="Cooking notes for sourdough bread.",
                description={"sections": []},
                extra="",
                ingest_status="done",
                ingested_at=now,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(FileEntry(
                id=cooking_entry_id,
                folder_id=None,
                file_id=cooking_file_id,
                display_name="cooking.txt",
                lifecycle="active",
                catalog_id=None,
                extra="",
                deleted_at=None,
                purge_after=None,
                created_at=now,
                updated_at=now,
            ))
            await session.commit()

        async with factory() as session:
            result = await build_semantic_index(
                session,
                entry_ids=[raft_entry_id, cooking_entry_id],
                client=_FakeEmbeddingClient(),
                progress_every=0,
            )

        assert result.entries_indexed == 2
        assert result.dimensions == 3
        if sqlite_vec_available():
            assert (semantic_index_dir() / SQLITE_VEC_INDEX_FILENAME).exists()

        hits = await search_semantic_index(
            "leader election",
            limit=2,
            client=_FakeEmbeddingClient(),
        )

        assert [hit.entry_id for hit in hits] == [raft_entry_id, cooking_entry_id]

        many = await search_semantic_index_many(
            ["leader election", "sourdough starter"],
            limit=1,
            client=_FakeEmbeddingClient(),
        )
        assert [[hit.entry_id for hit in group] for group in many] == [
            [raft_entry_id],
            [cooking_entry_id],
        ]
    finally:
        await engine.dispose()


def test_embedding_client_does_not_reuse_vision_key() -> None:
    settings = Settings(
        embedding_provider="openai-compatible",
        embedding_api_key=None,
        llm_vision_api_key="vision-key",
        llm_vision_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    with pytest.raises(EmbeddingConfigError):
        get_embedding_client(settings)


def test_parse_rerank_hits_handles_bailian_response() -> None:
    hits = _parse_rerank_hits({
        "results": [
            {"index": 2, "relevance_score": 0.91},
            {"index": "0", "relevance_score": "0.42"},
        ],
    })

    assert [(hit.index, hit.score, hit.rank) for hit in hits] == [
        (2, 0.91, 1),
        (0, 0.42, 2),
    ]
