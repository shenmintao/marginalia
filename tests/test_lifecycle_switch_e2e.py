"""AUTO_LIFECYCLE_ENABLED=false prevents automatic lifecycle transitions.

Run:
    .venv/Scripts/python tests/test_lifecycle_switch_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_lifecycle_switch_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["AUTO_LIFECYCLE_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text

from marginalia.config import get_settings

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, FileEntry, Folder
from marginalia.tasks.handlers.suggest_lifecycle import handle_suggest_lifecycle
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_old_active_entry() -> str:
    factory = get_session_factory()
    now = _now()
    old = now - timedelta(days=90)
    async with factory() as s:
        folder = Folder(
            id=new_id(), parent_id=None, name="root",
            created_at=now, updated_at=now,
        )
        s.add(folder)
        file = File(
            id=new_id(),
            storage_key="00/aa/lifecycle.txt",
            sha256="a" * 64,
            size_bytes=12,
            mime_type="text/plain",
            original_ext=".txt",
            kind="text",
            summary="old active entry",
            description={"sections": []},
            extra=None,
            ingest_status="done",
            ingested_at=old,
            created_at=old,
            updated_at=old,
        )
        s.add(file)
        await s.flush()
        entry = FileEntry(
            id=new_id(),
            folder_id=folder.id,
            file_id=file.id,
            display_name="old.txt",
            lifecycle="active",
            catalog_id=None,
            extra=None,
            created_at=old,
            updated_at=old,
        )
        s.add(entry)
        await s.commit()
        return entry.id


async def _entry_lifecycle(entry_id: str) -> str:
    factory = get_session_factory()
    async with factory() as s:
        return (await s.execute(
            select(FileEntry.lifecycle).where(FileEntry.id == entry_id)
        )).scalar_one()


async def main() -> None:
    await _create_schema()
    entry_id = await _seed_old_active_entry()

    await handle_suggest_lifecycle({"phases": ["demote"]})
    state = await _entry_lifecycle(entry_id)
    assert state == "active", f"disabled lifecycle should not demote, got {state}"

    factory = get_session_factory()
    async with factory() as s:
        raw = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='suggest_demotion' AND object_kind='global' "
            "ORDER BY completed_at DESC LIMIT 1"
        ))).scalar_one()
    detail = json.loads(raw) if isinstance(raw, str) else raw
    assert detail["disabled"] is True
    assert detail["reason"] == "AUTO_LIFECYCLE_ENABLED=false"
    print("[1] auto lifecycle disabled: no transition, outcome recorded")

    print("\nALL LIFECYCLE SWITCH E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
