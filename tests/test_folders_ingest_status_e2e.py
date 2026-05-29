"""ingest_status surfaces through the folder-listing API.

The library row paints a "failed" badge from this field, so the value
the GUI gets here is load-bearing — if it's missing, every row looks
healthy regardless of underlying state.

Asserts:
  1. GET /v1/folders (root listing) carries `ingest_status` for each
     root-level entry.
  2. GET /v1/folders/{id} carries `ingest_status` for every entry inside
     the folder, with values matching the seeded `File.ingest_status`.
  3. The four legal status values (pending / processing / done / failed)
     all round-trip unchanged.
  4. Folder rows carry recursive `ingest_summary` values so collapsed
     directories can show unfinished descendants.
  5. Failed file rows carry the latest ingest task error so the GUI can
     explain the red warning icon.

Run:
    .venv/Scripts/python tests/test_folders_ingest_status_e2e.py
"""
from __future__ import annotations

import asyncio
import atexit
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_TEST_PARENT = Path(os.environ.get(
    "MARGINALIA_TEST_TMP",
    str(Path(__file__).resolve().parent),
))
_TEST_PARENT.mkdir(parents=True, exist_ok=True)
_TEST_ROOT = _TEST_PARENT / f"_folders_ingest_status_e2e_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True)
atexit.register(lambda: shutil.rmtree(_TEST_ROOT, ignore_errors=True))
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import AuditEvent, Base, File, FileEntry, Folder, Task
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    """Seed direct entry statuses plus a nested-only pending branch.

    The nested branch verifies that folder rows summarize descendant files,
    not just direct children.
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="Reports")
        s.add(folder); await s.flush()

        statuses = ["pending", "processing", "done", "failed"]
        entries: dict[str, str] = {}  # display_name -> status
        folder_failed_error = "RuntimeError: markdown decode failed"
        for st in statuses:
            f = File(
                id=new_id(), storage_key=f"sk-{new_id()}",
                sha256=("a" * 64), size_bytes=10,
                ingest_status=st,
            )
            s.add(f); await s.flush()
            e = FileEntry(
                id=new_id(), folder_id=folder.id, file_id=f.id,
                display_name=f"{st}.txt", lifecycle="active",
            )
            s.add(e)
            entries[e.display_name] = st
            if st == "failed":
                s.add(Task(
                    id=new_id(),
                    kind="ingest_file",
                    payload={"file_id": f.id},
                    status="dead",
                    last_error=folder_failed_error,
                    scheduled_at=now,
                    created_at=now,
                    finished_at=now,
                ))

        # Root-level "failed" file — mirror the GUI's mixed root tree.
        root_failed_error = "ValueError: no pipeline could read this file"
        root_f = File(
            id=new_id(), storage_key=f"sk-{new_id()}",
            sha256=("b" * 64), size_bytes=20,
            ingest_status="failed",
        )
        s.add(root_f); await s.flush()
        root_e = FileEntry(
            id=new_id(), folder_id=None, file_id=root_f.id,
            display_name="orphan.txt", lifecycle="active",
        )
        s.add(root_e)
        s.add(Task(
            id=new_id(),
            kind="ingest_file",
            payload={"file_id": root_f.id},
            status="dead",
            last_error=root_failed_error,
            scheduled_at=now,
            created_at=now,
            finished_at=now,
        ))

        audit_only_error = "no_live_entry"
        audit_only_f = File(
            id=new_id(), storage_key=f"sk-{new_id()}",
            sha256=("d" * 64), size_bytes=25,
            ingest_status="failed",
        )
        s.add(audit_only_f); await s.flush()
        audit_only_e = FileEntry(
            id=new_id(), folder_id=None, file_id=audit_only_f.id,
            display_name="audit-only-failed.txt", lifecycle="active",
        )
        s.add(audit_only_e)
        s.add(AuditEvent(
            id=new_id(),
            occurred_at=now,
            kind="ingest_status_changed",
            payload={
                "file_id": audit_only_f.id,
                "status": "failed",
                "reason": audit_only_error,
            },
        ))

        nested_parent = Folder(id=new_id(), parent_id=None, name="NestedOnly")
        s.add(nested_parent); await s.flush()
        nested_child = Folder(
            id=new_id(), parent_id=nested_parent.id, name="NeedsWork"
        )
        s.add(nested_child); await s.flush()
        nested_f = File(
            id=new_id(), storage_key=f"sk-{new_id()}",
            sha256=("c" * 64), size_bytes=30,
            ingest_status="pending",
        )
        s.add(nested_f); await s.flush()
        nested_e = FileEntry(
            id=new_id(), folder_id=nested_child.id, file_id=nested_f.id,
            display_name="nested-pending.txt", lifecycle="active",
        )
        s.add(nested_e)

        await s.commit()
        return {
            "folder_id": folder.id,
            "nested_parent_id": nested_parent.id,
            "nested_child_id": nested_child.id,
            "entries": entries,
            "folder_failed_error": folder_failed_error,
            "root_failed_error": root_failed_error,
            "audit_only_error": audit_only_error,
        }


async def test_ingest_status_surfaces() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # Root listing — orphan.txt with failed status must be visible.
            r = await c.get("/v1/folders")
            assert r.status_code == 200, r.text
            root_entries = r.json()["entries"]
            assert root_entries, "root listing has no entries"
            orphan = next((e for e in root_entries if e["display_name"] == "orphan.txt"), None)
            assert orphan is not None, "orphan.txt missing from root listing"
            assert orphan["ingest_status"] == "failed", orphan
            assert orphan["ingest_error"] == seeded["root_failed_error"], orphan
            audit_only = next(
                (
                    e for e in root_entries
                    if e["display_name"] == "audit-only-failed.txt"
                ),
                None,
            )
            assert audit_only is not None, "audit-only-failed.txt missing"
            assert audit_only["ingest_error"] == seeded["audit_only_error"], audit_only
            print("[1] root listing surfaces ingest_status=failed")

            root_folders = {f["name"]: f for f in r.json()["folders"]}
            nested_parent = root_folders["NestedOnly"]
            assert nested_parent["ingest_summary"] == {
                "total": 1,
                "pending": 1,
                "processing": 0,
                "done": 0,
                "failed": 0,
                "incomplete": 1,
                "status": "pending",
            }, nested_parent
            print("[1b] root folder rows summarize descendant ingest status")

            # Folder detail — all four statuses round-trip.
            r = await c.get(f"/v1/folders/{seeded['folder_id']}")
            assert r.status_code == 200, r.text
            got = {e["display_name"]: e["ingest_status"] for e in r.json()["entries"]}
            assert got == seeded["entries"], (got, seeded["entries"])
            print("[2] folder detail surfaces all four statuses correctly")
            failed = next(e for e in r.json()["entries"] if e["display_name"] == "failed.txt")
            assert failed["ingest_error"] == seeded["folder_failed_error"], failed
            print("[2b] failed entries carry the latest ingest task error")

            # Sanity: shape carries other expected fields too.
            sample = r.json()["entries"][0]
            for key in ("id", "folder_id", "file_id", "display_name", "lifecycle"):
                assert key in sample, (key, sample)
            print("[3] entry payload preserves existing fields")

            r = await c.get(f"/v1/folders/{seeded['nested_parent_id']}")
            assert r.status_code == 200, r.text
            child = next(c for c in r.json()["children"] if c["name"] == "NeedsWork")
            assert child["ingest_summary"]["status"] == "pending", child
            assert child["ingest_summary"]["incomplete"] == 1, child
            assert r.json()["ingest_summary"]["status"] == "pending", r.json()
            print("[4] folder detail recursively summarizes child folders")


async def main() -> None:
    await _create_schema()
    await test_ingest_status_surfaces()
    print("\nALL FOLDERS-INGEST-STATUS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
