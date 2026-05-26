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

Run:
    .venv/Scripts/python tests/test_folders_ingest_status_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_folders_ingest_status_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
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
from marginalia.db.models import Base, File, FileEntry, Folder
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    """One folder with four entries, each backed by a File row in a
    distinct ingest_status. Plus one root-level entry whose file failed
    ingest, to cover the parent_id=None path."""
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="Reports")
        s.add(folder); await s.flush()

        statuses = ["pending", "processing", "done", "failed"]
        entries: dict[str, str] = {}  # display_name -> status
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

        # Root-level "failed" file — mirror the GUI's mixed root tree.
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

        await s.commit()
        return {"folder_id": folder.id, "entries": entries}


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
            print("[1] root listing surfaces ingest_status=failed")

            # Folder detail — all four statuses round-trip.
            r = await c.get(f"/v1/folders/{seeded['folder_id']}")
            assert r.status_code == 200, r.text
            got = {e["display_name"]: e["ingest_status"] for e in r.json()["entries"]}
            assert got == seeded["entries"], (got, seeded["entries"])
            print("[2] folder detail surfaces all four statuses correctly")

            # Sanity: shape carries other expected fields too.
            sample = r.json()["entries"][0]
            for key in ("id", "folder_id", "file_id", "display_name", "lifecycle"):
                assert key in sample, (key, sample)
            print("[3] entry payload preserves existing fields")


async def main() -> None:
    await _create_schema()
    await test_ingest_status_surfaces()
    print("\nALL FOLDERS-INGEST-STATUS CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
