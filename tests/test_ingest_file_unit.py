from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marginalia.db.models import Base, EntryTag, File, FileEntry, Tag
from marginalia.pipelines.base import PipelineResult, TagSuggestion
from marginalia.tasks.handlers.ingest_file import _persist
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_ingest_persist_dedupes_tags_resolved_in_same_transaction(
    tmp_path,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ingest.db'}")
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    now = _now()
    file_id = new_id()
    entry_id = new_id()
    canonical_id = new_id()
    alias_id = new_id()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with factory() as session:
            session.add(File(
                id=file_id,
                storage_key="00/aa/ingest",
                sha256="a" * 64,
                size_bytes=10,
                mime_type="text/plain",
                original_ext=".txt",
                kind=None,
                summary=None,
                description=None,
                extra=None,
                ingest_status="processing",
                ingested_at=None,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(FileEntry(
                id=entry_id,
                folder_id=None,
                file_id=file_id,
                display_name="note.txt",
                lifecycle="active",
                catalog_id=None,
                extra=None,
                deleted_at=None,
                purge_after=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(Tag(
                id=canonical_id,
                name="llm",
                facet="topic",
                alias_of=None,
                doc_count=0,
                last_used_at=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(Tag(
                id=alias_id,
                name="large language model",
                facet="topic",
                alias_of=canonical_id,
                doc_count=0,
                last_used_at=None,
                created_at=now,
                updated_at=now,
            ))
            await session.commit()

        result = PipelineResult(
            summary="A note about indexing language-model material.",
            description={"sections": []},
            kind="text",
            extra=None,
            entry_extra=None,
            entry_catalog_path=None,
            entry_tags=[
                TagSuggestion(name="llm", facet="topic"),
                TagSuggestion(name="llm", facet="topic"),
                TagSuggestion(name="large language model", facet="topic"),
                TagSuggestion(name="retrieval", facet="topic"),
                TagSuggestion(name="retrieval", facet="topic"),
            ],
        )

        async with factory() as session:
            await _persist(session, file_id=file_id, entry_id=entry_id, result=result)
            await session.commit()

        async with factory() as session:
            pairs = (
                await session.execute(
                    select(EntryTag.entry_id, EntryTag.tag_id)
                    .where(EntryTag.entry_id == entry_id)
                    .order_by(EntryTag.tag_id)
                )
            ).all()
            assert len(pairs) == 2
            assert (entry_id, canonical_id) in pairs

            retrieval = (
                await session.execute(
                    select(Tag).where(Tag.name == "retrieval", Tag.facet == "topic")
                )
            ).scalar_one()
            assert (entry_id, retrieval.id) in pairs

            canonical = await session.get(Tag, canonical_id)
            alias = await session.get(Tag, alias_id)
            assert canonical is not None
            assert alias is not None
            assert canonical.doc_count == 1
            assert alias.doc_count == 0
            assert retrieval.doc_count == 1
    finally:
        await engine.dispose()
