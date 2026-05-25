"""End-to-end purge_deleted_files sanity check.

Run:
    .venv/Scripts/python tests/test_purge_e2e.py

Verifies:
  1. file with 2 entries; soft-delete entry A with purge_after in the past
     → purge deletes entry A; file row + storage object remain (entry B still references)
  2. soft-delete entry B with purge_after in the past
     → purge deletes entry B + the file row; storage object is removed
  3. soft-deleted entry whose purge_after is still in the future is left alone
  4. audit kinds: entry_purged (×3, one per purged entry), file_purged (×1)
     — purge_deleted_files_completed is now in task_outcomes, not audit
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_purge_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, FileEntry, Folder
from marginalia.storage import get_storage
from marginalia.tasks.handlers.purge_deleted_files import handle_purge_deleted_files
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Seed: 1 folder, 1 file, 2 entries (both initially live), 1 storage object."""
    factory = get_session_factory()
    storage = get_storage()
    now = _now()

    # write a fake storage object
    storage_key = "00/aa/test-blob"
    async def _stream():
        yield b"hello marginalia"
    await storage.put(storage_key, _stream(), content_type="text/plain")

    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder)

        f = File(
            id=new_id(),
            storage_key=storage_key,
            sha256="z" * 64,
            size_bytes=16,
            mime_type="text/plain",
            original_ext=".txt",
            kind="text",
            summary="x", description={"sections": []}, extra=None,
            ingest_status="done", ingested_at=now,
            created_at=now, updated_at=now,
        )
        s.add(f)
        await s.flush()

        e1 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="alpha.txt", lifecycle="active",
                       catalog_id=None, extra=None,
                       created_at=now, updated_at=now)
        e2 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="beta.txt", lifecycle="active",
                       catalog_id=None, extra=None,
                       created_at=now, updated_at=now)
        e3 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="future.txt", lifecycle="active",
                       catalog_id=None, extra=None,
                       created_at=now, updated_at=now)
        s.add_all([e1, e2, e3])
        await s.commit()

        return {
            "file_id": f.id, "storage_key": storage_key,
            "entry_a": e1.id, "entry_b": e2.id, "entry_future": e3.id,
        }


async def _soft_delete(entry_id: str, purge_after_offset: timedelta) -> None:
    factory = get_session_factory()
    async with factory() as s:
        e = await s.get(FileEntry, entry_id)
        e.deleted_at = _now()
        e.purge_after = _now() + purge_after_offset
        e.updated_at = _now()
        await s.commit()


async def main():
    await _create_schema()
    seeded = await _seed()
    factory = get_session_factory()
    storage = get_storage()

    # --- 1. soft-delete entry A with already-past purge_after ---
    await _soft_delete(seeded["entry_a"], timedelta(seconds=-60))
    # also soft-delete entry_future but with FUTURE purge_after — must NOT be touched
    await _soft_delete(seeded["entry_future"], timedelta(days=30))

    await handle_purge_deleted_files({})

    async with factory() as s:
        ea = await s.get(FileEntry, seeded["entry_a"])
        eb = await s.get(FileEntry, seeded["entry_b"])
        ef = await s.get(FileEntry, seeded["entry_future"])
        f = await s.get(File, seeded["file_id"])
        assert ea is None, "entry_a should be physically deleted"
        assert eb is not None and eb.deleted_at is None, "entry_b must still be live"
        assert ef is not None and ef.deleted_at is not None, "entry_future was purged prematurely!"
        assert f is not None, "file must still exist while entry_b/future remain"
    print("[1] entry_a purged; file + entry_b + entry_future intact")

    # storage object should still exist
    assert await storage.exists(seeded["storage_key"]), "storage blob deleted prematurely"

    # --- 2. soft-delete entry B (now the only live entry) with past purge ---
    await _soft_delete(seeded["entry_b"], timedelta(seconds=-60))

    await handle_purge_deleted_files({})

    async with factory() as s:
        eb = await s.get(FileEntry, seeded["entry_b"])
        ef = await s.get(FileEntry, seeded["entry_future"])
        f = await s.get(File, seeded["file_id"])
        # entry_b is gone. entry_future still untouched (future). Because it's
        # still around, the file should ALSO still be around (still has a row).
        assert eb is None
        assert ef is not None
        assert f is not None
    print("[2] entry_b purged; file kept because entry_future still references it")
    assert await storage.exists(seeded["storage_key"]), "storage blob deleted while entry_future still references file"

    # --- 3. fast-forward: pretend purge_after on entry_future passed --------
    await _soft_delete(seeded["entry_future"], timedelta(seconds=-60))
    await handle_purge_deleted_files({})

    async with factory() as s:
        ef = await s.get(FileEntry, seeded["entry_future"])
        f = await s.get(File, seeded["file_id"])
        assert ef is None
        assert f is None, "file should be gone now that no entries reference it"
    assert not await storage.exists(seeded["storage_key"]), "storage blob still present after file purge"
    print("[3] entry_future purged; file row + storage object both gone")

    # --- 4. audit invariants ------------------------------------------------
    async with factory() as s:
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events GROUP BY kind ORDER BY kind"
        ))).all()
        kind_counts = {k: c for k, c in kinds}
        print("[4] audit counts:", kind_counts)
        assert kind_counts.get("entry_purged", 0) == 3
        assert kind_counts.get("file_purged", 0) == 1
        # purge_deleted_files writes its run summary to task_outcomes now.
        outcomes = (await s.execute(text(
            "SELECT outcome, COUNT(*) FROM task_outcomes "
            "WHERE task_kind='purge_deleted_files' GROUP BY outcome"
        ))).all()
        print("[4] purge_deleted_files task_outcomes:", outcomes)
        outcome_counts = {o: c for o, c in outcomes}
        # 3 invocations: 1st applied, 2nd applied, 3rd applied
        assert sum(outcome_counts.values()) == 3

    print("\nALL PURGE E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
