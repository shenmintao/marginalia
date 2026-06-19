from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from marginalia.db.bootstrap import _reconcile_dead_ingest_files
from marginalia.db.models import AuditEvent, Base, File, Task
from marginalia.services.ingest_status import mark_file_failed_for_dead_ingest_task
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file(file_id: str, *, status: str = "processing") -> File:
    now = _now()
    return File(
        id=file_id,
        storage_key=f"store/{file_id}",
        sha256=file_id.replace("-", "")[:64].ljust(64, "0"),
        size_bytes=10,
        mime_type="text/plain",
        original_ext=".txt",
        kind=None,
        summary=None,
        description=None,
        extra=None,
        ingest_status=status,
        ingested_at=None,
        deleted_at=None,
        created_at=now,
        updated_at=now,
    )


def _task(
    task_id: str,
    *,
    file_id: str,
    status: str,
    kind: str = KIND_INGEST_FILE,
) -> Task:
    now = _now()
    return Task(
        id=task_id,
        kind=kind,
        payload={"file_id": file_id},
        dedup_key=f"ingest_file:{file_id}",
        status=status,
        priority=100,
        attempts=1,
        max_attempts=5,
        last_error="boom" if status == "dead" else None,
        scheduled_at=now,
        lease_expires_at=None,
        last_heartbeat_at=None,
        locked_by=None,
        created_at=now,
        started_at=now if status in {"running", "dead", "done"} else None,
        finished_at=now if status in {"dead", "done"} else None,
    )


@pytest.mark.asyncio
async def test_dead_ingest_task_marks_file_failed_and_audits(tmp_path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    file_id = new_id()
    task_id = new_id()

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async with factory() as session:
            session.add(_file(file_id))
            await session.commit()

        async with factory() as session:
            changed = await mark_file_failed_for_dead_ingest_task(
                session,
                task_id=task_id,
                kind=KIND_INGEST_FILE,
                payload={"file_id": file_id},
                reason="task exploded",
            )
            await session.commit()
            assert changed is True

        async with factory() as session:
            file_row = await session.get(File, file_id)
            assert file_row is not None
            assert file_row.ingest_status == "failed"

            event = (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.kind == "ingest_status_changed")
                    .order_by(AuditEvent.occurred_at.desc())
                )
            ).scalar_one()
            assert event.task_id == task_id
            assert event.payload["file_id"] == file_id
            assert event.payload["status"] == "failed"
            assert event.payload["reason"] == "task exploded"
    finally:
        await engine.dispose()


def test_bootstrap_reconciles_dead_ingest_files_without_active_task(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    dead_only = new_id()
    active_too = new_id()
    no_dead = new_id()

    try:
        Base.metadata.create_all(engine)
        from sqlalchemy.orm import Session

        with Session(engine) as session:
            session.add_all([
                _file(dead_only),
                _file(active_too),
                _file(no_dead),
                _task(new_id(), file_id=dead_only, status="dead"),
                _task(new_id(), file_id=active_too, status="dead"),
                _task(new_id(), file_id=active_too, status="running"),
            ])
            session.commit()

        with engine.begin() as conn:
            _reconcile_dead_ingest_files(conn)

        with Session(engine) as session:
            assert session.get(File, dead_only).ingest_status == "failed"
            assert session.get(File, active_too).ingest_status == "processing"
            assert session.get(File, no_dead).ingest_status == "processing"
    finally:
        engine.dispose()
