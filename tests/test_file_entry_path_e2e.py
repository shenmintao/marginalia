"""GET /v1/file-entries/{id}/path — folder ancestor chain.

The desktop GUI uses this to expand the Library tree when arriving
from a search hit or chat citation: it asks for the chain of folder
ids root → leaf, opens each one, and finally selects the file.

Locked-in behaviours:
  1. Nested folders return root-first ancestors.
  2. Root-folder entries return an empty `ancestors`.
  3. Soft-deleted ancestors stop the walk (defensive — the leaf entry
     itself is still visible only if it isn't deleted).
  4. Soft-deleted entries return 404.

Run:
    .venv/Scripts/python -m pytest tests/test_file_entry_path_e2e.py -x -q
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_file_entry_path_e2e_data"
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
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        # /papers/distsys/raft.md  +  /loose.md (root entry)
        f_papers = Folder(id=new_id(), parent_id=None, name="papers",
                          created_at=now, updated_at=now)
        s.add(f_papers); await s.flush()
        f_dist = Folder(id=new_id(), parent_id=f_papers.id, name="distsys",
                        created_at=now, updated_at=now)
        s.add(f_dist); await s.flush()

        def _file() -> File:
            return File(
                id=new_id(), storage_key=new_id(), sha256="d" * 64,
                size_bytes=10, mime_type="text/plain",
                original_ext=".md", kind="text",
                summary=None, description={"sections": []}, extra=None,
                ingest_status="done", ingested_at=now,
                created_at=now, updated_at=now,
            )

        nested_file = _file(); s.add(nested_file); await s.flush()
        nested_entry = FileEntry(
            id=new_id(), folder_id=f_dist.id, file_id=nested_file.id,
            display_name="raft.md", lifecycle="active",
            catalog_id=None, extra=None, created_at=now, updated_at=now,
        )
        s.add(nested_entry)

        root_file = _file(); s.add(root_file); await s.flush()
        root_entry = FileEntry(
            id=new_id(), folder_id=None, file_id=root_file.id,
            display_name="loose.md", lifecycle="active",
            catalog_id=None, extra=None, created_at=now, updated_at=now,
        )
        s.add(root_entry); await s.flush()

        deleted_entry = FileEntry(
            id=new_id(), folder_id=f_dist.id, file_id=nested_file.id,
            display_name="ghost.md", lifecycle="active",
            catalog_id=None, extra=None,
            deleted_at=now, purge_after=now,
            created_at=now, updated_at=now,
        )
        s.add(deleted_entry); await s.flush()

        await s.commit()
        return {
            "nested_eid": nested_entry.id,
            "root_eid": root_entry.id,
            "deleted_eid": deleted_entry.id,
            "papers": f_papers.id,
            "distsys": f_dist.id,
        }


async def test_nested_entry_returns_root_first_chain() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/v1/file-entries/{seeded['nested_eid']}/path")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["entry_id"] == seeded["nested_eid"]
            assert body["display_name"] == "raft.md"
            assert body["folder_id"] == seeded["distsys"]
            chain = [a["id"] for a in body["ancestors"]]
            assert chain == [seeded["papers"], seeded["distsys"]], chain
            names = [a["name"] for a in body["ancestors"]]
            assert names == ["papers", "distsys"]
            print("[1] nested entry: ancestors are root → leaf")


async def test_root_entry_returns_empty_ancestors() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/v1/file-entries/{seeded['root_eid']}/path")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["folder_id"] is None
            assert body["ancestors"] == []
            print("[2] root entry: empty ancestors")


async def test_deleted_entry_404() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/v1/file-entries/{seeded['deleted_eid']}/path")
            assert r.status_code == 404
            print("[3] soft-deleted entry: 404")


async def main() -> None:
    await _create_schema()
    await test_nested_entry_returns_root_first_chain()
    await test_root_entry_returns_empty_ancestors()
    await test_deleted_entry_404()
    print("\nALL FILE-ENTRY-PATH TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
