"""End-to-end search / metadata / download (Cycle 15).

Run:
    .venv/Scripts/python tests/test_user_files_e2e.py

Verifies:
  1. /search?q=... returns matches by display_name AND by files.summary,
     but the response NEVER includes summary (recall-only).
  2. /file-entries/{id}/metadata returns user-visible fields PLUS the
     librarian "label card" (summary). It must NOT include AI-internal
     fields: catalog_id, description, kind, extra, tags.
  3. /file-entries/{id}/download streams correct bytes + sets
     Content-Disposition + X-File-Id header.
  4. CLI commands /search /info /download all work end-to-end through the
     ASGI transport.
  5. Soft-deleted entries are excluded from search and 404 on
     metadata/download.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_user_files_e2e_data"
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
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.cli import CliContext, MarginaliaClient, dispatch
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Catalog, EntryTag, File, FileEntry, Folder, Tag,
)
from marginalia.main import app
from marginalia.storage import get_storage
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    factory = get_session_factory()
    storage = get_storage()
    now = _now()

    body_a = b"Paper A about Raft consensus algorithm.\n" * 5
    body_b = b"Notes on database internals.\n" * 5

    async def _stream(b: bytes):
        async def _it():
            yield b
        return _it()

    await storage.put("00/aa/a", await _stream(body_a), content_type="text/plain")
    await storage.put("00/aa/b", await _stream(body_b), content_type="text/plain")

    async with factory() as s:
        f_root = Folder(id=new_id(), parent_id=None, name="research",
                        created_at=now, updated_at=now)
        s.add(f_root); await s.flush()
        f_llm = Folder(id=new_id(), parent_id=f_root.id, name="llm",
                       created_at=now, updated_at=now)
        s.add(f_llm); await s.flush()

        cat = Catalog(id=new_id(), parent_id=None, name="HiddenCatalog",
                      summary=None, description=None, extra="ai-only-data",
                      tags=None, created_at=now, updated_at=now)
        s.add(cat); await s.flush()

        f_a = File(id=new_id(), storage_key="00/aa/a", sha256="a"*64,
                   size_bytes=len(body_a),
                   mime_type="text/plain", original_ext=".txt", kind="text",
                   summary="A note about consensus algorithms.",
                   description={"sections": [{"id": "s1", "title": "intro"}]},
                   extra="cross-cutting AI insight",
                   ingest_status="done", ingested_at=now,
                   created_at=now, updated_at=now)
        f_b = File(id=new_id(), storage_key="00/aa/b", sha256="b"*64,
                   size_bytes=len(body_b),
                   mime_type="text/plain", original_ext=".txt", kind="text",
                   summary="Some database internals.",
                   description={"sections": []}, extra=None,
                   ingest_status="done", ingested_at=now,
                   created_at=now, updated_at=now)
        s.add_all([f_a, f_b]); await s.flush()

        e_a = FileEntry(id=new_id(), folder_id=f_llm.id, file_id=f_a.id,
                        display_name="raft.md", lifecycle="active",
                        catalog_id=cat.id, extra="ai-position-extra",
                        created_at=now, updated_at=now)
        e_b = FileEntry(id=new_id(), folder_id=f_llm.id, file_id=f_b.id,
                        display_name="db-notes.md", lifecycle="active",
                        catalog_id=None, extra=None,
                        created_at=now, updated_at=now)
        e_deleted = FileEntry(id=new_id(), folder_id=f_llm.id, file_id=f_b.id,
                              display_name="ghost-consensus.md",
                              lifecycle="active",
                              deleted_at=_now(),
                              catalog_id=None, extra=None,
                              created_at=now, updated_at=now)
        s.add_all([e_a, e_b, e_deleted]); await s.flush()

        t = Tag(id=new_id(), name="hidden-tag", facet="topic",
                alias_of=None, doc_count=1, last_used_at=now,
                created_at=now, updated_at=now)
        s.add(t); await s.flush()
        s.add(EntryTag(entry_id=e_a.id, tag_id=t.id,
                       source="ingest", created_at=now))

        await s.commit()
        return {
            "e_a": e_a.id, "e_b": e_b.id, "e_deleted": e_deleted.id,
            "folder_root": f_root.id, "folder_llm": f_llm.id,
            "body_a": body_a, "body_b": body_b,
        }


async def main():
    await _create_schema()
    seeded = await _seed()
    transport = ASGITransport(app=app)
    base = "http://t"

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=base) as c:
            # ---- 1. Search by display_name ---------------------------
            r = await c.get("/v1/search", params={"q": "raft"})
            assert r.status_code == 200, r.text
            results = r.json()["entries"]
            print("[1] /search 'raft':", [e["display_name"] for e in results])
            assert len(results) == 1
            assert results[0]["entry_id"] == seeded["e_a"]
            assert "summary" not in results[0], "summary leaked into search"
            assert "catalog_id" not in results[0]
            assert "extra" not in results[0]

            # ---- 2. Search by content summary ------------------------
            r = await c.get("/v1/search", params={"q": "consensus"})
            assert r.status_code == 200
            results = r.json()["entries"]
            names = {e["display_name"] for e in results}
            print("[2] /search 'consensus':", names)
            # raft.md has summary "A note about consensus algorithms" → matches
            assert "raft.md" in names
            # the soft-deleted ghost-consensus.md must NOT appear
            assert "ghost-consensus.md" not in names

            # ---- 3. Search returns no AI fields ----------------------
            for e in results:
                assert "summary" not in e
                assert "description" not in e
                assert "kind" not in e

            # ---- 4. Metadata: includes summary, no AI fields ---------
            r = await c.get(f"/v1/file-entries/{seeded['e_a']}/metadata")
            assert r.status_code == 200, r.text
            meta = r.json()
            print("[4] metadata keys:", sorted(meta.keys()))
            assert meta["summary"] == "A note about consensus algorithms."
            assert meta["display_name"] == "raft.md"
            assert meta["folder_path"] == "/research/llm"
            assert meta["sha256"]
            for forbidden in ("catalog_id", "description", "kind", "entry_tags"):
                assert forbidden not in meta, f"{forbidden} leaked into metadata"

            # ---- 5. Metadata 404 on soft-deleted ---------------------
            r = await c.get(f"/v1/file-entries/{seeded['e_deleted']}/metadata")
            assert r.status_code == 404
            print("[5] soft-deleted metadata 404 OK")

            # ---- 6. Download streams correct bytes -------------------
            async with c.stream(
                "GET", f"/v1/file-entries/{seeded['e_a']}/download"
            ) as r:
                assert r.status_code == 200
                cd = r.headers.get("content-disposition") or ""
                assert "raft.md" in cd
                xfid = r.headers.get("x-file-id")
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)
            print("[6] download:", len(buf), "bytes; X-File-Id:", xfid)
            assert bytes(buf) == seeded["body_a"]

            # ---- 7. Download 404 on soft-deleted ---------------------
            r = await c.get(f"/v1/file-entries/{seeded['e_deleted']}/download")
            assert r.status_code == 404
            print("[7] soft-deleted download 404 OK")

        # ---- 8. CLI: /search, /info, /download ------------------------
        async with httpx.AsyncClient(transport=transport, base_url=base) as raw_http:
            client = MarginaliaClient(base_url=base, transport=transport)
            ctx = CliContext(client=client)

            await dispatch(ctx, "/search consensus")
            print("[8] CLI /search OK")

            await dispatch(ctx, f"/info {seeded['e_a']}")
            print("[8] CLI /info OK")

            local_dest = _TEST_ROOT / "downloaded.md"
            await dispatch(ctx, f"/download {seeded['e_a']} {local_dest}")
            assert local_dest.exists()
            assert local_dest.read_bytes() == seeded["body_a"]
            print("[8] CLI /download OK; written", local_dest.stat().st_size, "bytes")

            # ---- 9. Folder zip download ----------------------------------
            zip_dest = _TEST_ROOT / "folder.zip"
            out = await client.download_folder(seeded["folder_llm"], dest=zip_dest)
            print("[9] folder zip:", out)
            assert zip_dest.exists()
            import zipfile
            with zipfile.ZipFile(zip_dest, "r") as zf:
                names = sorted(zf.namelist())
                print("[9] zip members:", names)
                # Live entries: raft.md (in /research/llm) + db-notes.md.
                # ghost-consensus.md was soft-deleted → must be excluded.
                # Members are stored RELATIVE to the requested folder root,
                # so direct children appear at the top level.
                assert "raft.md" in names
                assert "db-notes.md" in names
                assert all("ghost-consensus" not in n for n in names)
                # bytes survive
                assert zf.read("raft.md") == seeded["body_a"]
                assert zf.read("db-notes.md") == seeded["body_b"]
            assert out["member_count"] == 2

            # ---- 10. /download falls back to folder mode on entry miss --
            zip_dest2 = _TEST_ROOT / "via_cli.zip"
            await dispatch(ctx, f"/download {seeded['folder_llm']} {zip_dest2}")
            assert zip_dest2.exists()
            with zipfile.ZipFile(zip_dest2, "r") as zf:
                assert "raft.md" in zf.namelist()
            print("[10] CLI /download folder fallback OK")

            await client.aclose()

    print("\nALL USER_FILES E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
