from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marginalia.db.bootstrap import bootstrap_schema_sync
from marginalia.db.fts import ENTRY_METADATA_FTS_TABLE
from marginalia.db.models import File, FileEntry
from marginalia.repositories import entries as entries_repo
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_entry_metadata_fts_backfills_and_tracks_updates(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fts.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = _now()
    file_id = new_id()
    entry_id = new_id()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(bootstrap_schema_sync)

        async with factory() as session:
            has_fts = (
                await session.execute(
                    text(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = :name"
                    ),
                    {"name": ENTRY_METADATA_FTS_TABLE},
                )
            ).scalar_one_or_none()
            if not has_fts:
                pytest.skip("SQLite build does not provide FTS5 trigram")

            session.add(File(
                id=file_id,
                storage_key="00/aa/fts",
                sha256="a" * 64,
                size_bytes=10,
                mime_type="text/plain",
                original_ext=".txt",
                kind="text",
                summary="Consensus notes mention the raft protocol.",
                description=None,
                extra="replicated log",
                ingest_status="done",
                ingested_at=now,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            ))
            session.add(FileEntry(
                id=entry_id,
                folder_id=None,
                file_id=file_id,
                display_name="paper.txt",
                lifecycle="active",
                catalog_id=None,
                extra="leader election",
                deleted_at=None,
                purge_after=None,
                created_at=now,
                updated_at=now,
            ))
            await session.commit()

        async with factory() as session:
            direct = (
                await session.execute(
                    text(
                        "SELECT entry_id FROM entry_metadata_fts "
                        "WHERE entry_metadata_fts MATCH :query"
                    ),
                    {"query": '"aft"'},
                )
            ).scalars().all()
            assert direct == [entry_id]

            rows = await entries_repo.search_filtered(
                session,
                text=["aft"],
                lifecycle=["active"],
                limit=10,
            )
            total = await entries_repo.count_filtered(
                session,
                text=["aft"],
                lifecycle=["active"],
            )
            assert [entry.id for entry, _file in rows] == [entry_id]
            assert total == 1

            entry = await session.get(FileEntry, entry_id)
            assert entry is not None
            entry.display_name = "paxos-notes.txt"
            entry.extra = "quorum reads"
            file_row = await session.get(File, file_id)
            assert file_row is not None
            file_row.summary = "No old consensus keyword remains here."
            await session.commit()

        async with factory() as session:
            new_match = (
                await session.execute(
                    text(
                        "SELECT entry_id FROM entry_metadata_fts "
                        "WHERE entry_metadata_fts MATCH :query"
                    ),
                    {"query": '"pax"'},
                )
            ).scalars().all()
            old_match = (
                await session.execute(
                    text(
                        "SELECT entry_id FROM entry_metadata_fts "
                        "WHERE entry_metadata_fts MATCH :query"
                    ),
                    {"query": '"aft"'},
                )
            ).scalars().all()
            assert new_match == [entry_id]
            assert old_match == []
    finally:
        await engine.dispose()
