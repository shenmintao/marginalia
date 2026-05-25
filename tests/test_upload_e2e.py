"""End-to-end upload sanity check.

Run:
    .venv/Scripts/python tests/test_upload_e2e.py

Verifies:
  1. New file upload   → file row + entry row + ingest_file task enqueued
  2. Auto-create dirs  → /a/b/c.txt creates folders a, b
  3. Sha256 dedup      → second upload of same content reuses file_id, NO new task
  4. on_conflict=rename → same display_name → suffixed " (1)"
  5. on_conflict=error  → 409 with existing entry id
  6. on_conflict=skip   → returns existing entry, skipped=true
  7. Audit events fire for folder_created / file_created / entry_created / task_enqueued
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import sys
from pathlib import Path

# Use isolated SQLite + storage for the smoke run (don't pollute real data/).
_TEST_ROOT = Path(__file__).resolve().parent / "_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"  # we don't want a runner picking up tasks
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select, text

# Import after env vars set so config picks them up.
from marginalia.config import get_settings  # noqa: E402
get_settings.cache_clear()  # type: ignore[attr-defined]
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import AuditEvent, Base, File, FileEntry, Folder  # noqa: E402
from marginalia.main import app  # noqa: E402
from marginalia.tasks.kinds import KIND_INGEST_FILE  # noqa: E402


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def main() -> None:
    await _create_schema()

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # 1. Health probe
            assert (await c.get("/health")).status_code == 200

            # 2. New upload — auto-creates /research/llm folders
            content_a = b"Marginalia E2E content A\n" * 50
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/research/llm/paper.pdf"},
                files={"file": ("paper.pdf", io.BytesIO(content_a), "application/pdf")},
            )
            assert r.status_code == 201, r.text
            up1 = r.json()
            print("[1] new upload:", up1)
            assert up1["display_name"] == "paper.pdf"
            assert up1["deduped"] is False
            assert up1["auto_renamed"] is False

            # 3. Sha256 dedup — same bytes, different remote path
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/research/llm/copy_of_paper.pdf"},
                files={"file": ("copy.pdf", io.BytesIO(content_a), "application/pdf")},
            )
            assert r.status_code == 201, r.text
            up2 = r.json()
            print("[2] dedup upload:", up2)
            assert up2["file_id"] == up1["file_id"], "sha256 dedup failed"
            assert up2["entry_id"] != up1["entry_id"]
            assert up2["deduped"] is True

            # 4. Name conflict — default policy (rename) → " (1)"
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/research/llm/paper.pdf"},
                files={"file": ("paper.pdf", io.BytesIO(content_a), "application/pdf")},
            )
            assert r.status_code == 201, r.text
            up3 = r.json()
            print("[3] rename:", up3)
            assert up3["display_name"] == "paper (1).pdf"
            assert up3["auto_renamed"] is True
            assert up3["deduped"] is True

            # 5. Name conflict — on_conflict=error → 409
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/research/llm/paper.pdf", "on_conflict": "error"},
                files={"file": ("paper.pdf", io.BytesIO(content_a), "application/pdf")},
            )
            assert r.status_code == 409, r.text
            err = r.json()["detail"]
            print("[4] error policy:", err)
            assert err["existing_entry_id"] == up1["entry_id"]
            assert err["display_name"] == "paper.pdf"

            # 6. Name conflict — on_conflict=skip → return existing entry
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/research/llm/paper.pdf", "on_conflict": "skip"},
                files={"file": ("paper.pdf", io.BytesIO(content_a), "application/pdf")},
            )
            assert r.status_code == 201, r.text
            up5 = r.json()
            print("[5] skip:", up5)
            assert up5["skipped"] is True
            assert up5["entry_id"] == up1["entry_id"]

            # 7. Different content → new file row, new ingest task
            content_b = b"Different content B\n" * 100
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/datasets/raw.csv"},
                files={"file": ("raw.csv", io.BytesIO(content_b), "text/csv")},
            )
            assert r.status_code == 201, r.text
            up6 = r.json()
            print("[6] new file in new tree:", up6)
            assert up6["file_id"] != up1["file_id"]
            assert up6["deduped"] is False

            # 7a. Ambiguous remote_path (no extension, no trailing slash)
            # → 400 with ambiguous_remote_path detail
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/repos/marginalia"},
                files={"file": ("LICENSE", io.BytesIO(b"MIT"), "text/plain")},
            )
            assert r.status_code == 400, r.text
            assert r.json()["detail"]["error"] == "ambiguous_remote_path"
            print("[6a] ambiguous path rejected:", r.json()["detail"]["error"])

            # 7b. Same path with trailing '/' → folder, file lands as LICENSE
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/repos/marginalia/"},
                files={"file": ("LICENSE", io.BytesIO(b"MIT"), "text/plain")},
            )
            assert r.status_code == 201, r.text
            up6b = r.json()
            assert up6b["display_name"] == "LICENSE"
            print("[6b] /repos/marginalia/ + LICENSE:", up6b["display_name"])

            # 7c. Same path WITHOUT trailing '/' but display_name override
            # → folder=/repos/marginalia, display_name=LICENSE
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/repos/marginalia",
                        "display_name": "COPYRIGHT"},
                files={"file": ("LICENSE", io.BytesIO(b"MIT"), "text/plain")},
            )
            assert r.status_code == 201, r.text
            up6c = r.json()
            assert up6c["display_name"] == "COPYRIGHT"
            print("[6c] explicit display_name override:", up6c["display_name"])

            # 8. Folder browsing
            r = await c.get("/v1/folders")
            roots = r.json()["folders"]
            print("[7] roots:", [f["name"] for f in roots])
            assert {f["name"] for f in roots} == {"research", "datasets", "repos"}

            research_id = next(f["id"] for f in roots if f["name"] == "research")
            r = await c.get(f"/v1/folders/{research_id}")
            research = r.json()
            assert research["children"][0]["name"] == "llm"
            llm_id = research["children"][0]["id"]
            r = await c.get(f"/v1/folders/{llm_id}")
            llm = r.json()
            print("[8] /research/llm contents:",
                  [(e["display_name"], e["lifecycle"]) for e in llm["entries"]])
            names = {e["display_name"] for e in llm["entries"]}
            assert names == {"paper.pdf", "copy_of_paper.pdf", "paper (1).pdf"}

    # --- DB-level invariants
    factory = get_session_factory()
    async with factory() as s:
        n_files = (await s.execute(text("SELECT COUNT(*) FROM files"))).scalar()
        n_entries = (await s.execute(text("SELECT COUNT(*) FROM file_entries"))).scalar()
        n_folders = (await s.execute(text("SELECT COUNT(*) FROM folders"))).scalar()
        n_tasks = (await s.execute(text(
            "SELECT COUNT(*) FROM tasks WHERE kind = :k"
        ), {"k": KIND_INGEST_FILE})).scalar()
        n_events = (await s.execute(text("SELECT COUNT(*) FROM audit_events"))).scalar()

        # Unique sha256s: content_a, content_b, b"MIT" (twice with same hash → 1)
        # Files: 3. Entries: 4 (paper, copy_of, paper(1), raw.csv) + 2 LICENSE/COPYRIGHT = 6
        # Folders: research, llm, datasets, repos, marginalia = 5
        # Ingest tasks: 3 (one per unique sha256)
        print(
            "[DB] files=%d entries=%d folders=%d ingest_tasks=%d audit_events=%d"
            % (n_files, n_entries, n_folders, n_tasks, n_events)
        )
        assert n_files == 3
        assert n_entries == 6
        assert n_folders == 5
        assert n_tasks == 3

        # Audit kinds we expect to have fired at least once
        kinds = (await s.execute(
            text("SELECT DISTINCT kind FROM audit_events ORDER BY kind")
        )).scalars().all()
        print("[DB] audit kinds:", kinds)
        for required in ("folder_created", "file_created", "entry_created", "task_enqueued"):
            assert required in kinds, f"missing audit kind: {required}"

    print("\nALL UPLOAD E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
