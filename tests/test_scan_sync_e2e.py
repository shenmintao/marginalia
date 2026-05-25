"""Vault scan + unified /ingest e2e.

Run:
    .venv/Scripts/python tests/test_scan_sync_e2e.py

Steps:
  1. Mirror backend, vault is `data/library/`.
  2. Upload three files via the upload service. They land on disk under
     the vault.
  3. Add a 4th file directly on disk (simulating a user dropping a
     file into Finder). /check should report 1 new.
  4. Rename a file's display via plain os.rename. /check should
     report 1 moved.
  5. Delete a file from disk. /check should report 1 missing.
  6. Edit a file in place (same path, different content). /check should
     report 1 modified.
  7. Run apply_all (the one /ingest --all calls under the hood).
     Final /check should be in_sync.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_scan_sync_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
_VAULT = _TEST_ROOT / "library"
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "mirror"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from sqlalchemy import select  # noqa: E402

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, FileEntry  # noqa: E402
from marginalia.services.scan import scan_vault  # noqa: E402
from marginalia.services.sync import apply_all, apply_moved  # noqa: E402
from marginalia.storage import get_storage, reset_storage_cache  # noqa: E402


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _upload(body: bytes, *, name: str, remote_path: str) -> str:
    from marginalia.services.upload import upload
    storage = get_storage()

    async def _stream():
        yield body

    factory = get_session_factory()
    async with factory() as db:
        result = await upload(
            db, storage,
            stream=_stream(), fallback_name=name,
            remote_path=remote_path,
            content_type="text/plain",
        )
        await db.commit()
        return result.entry_id


async def _main() -> None:
    reset_storage_cache()
    await _create_schema()

    # 1. Seed three files.
    e1 = await _upload(b"first body\n", name="alpha.txt", remote_path="/notes/")
    e2 = await _upload(b"second body\n", name="beta.txt", remote_path="/notes/")
    e3 = await _upload(b"third body\n", name="gamma.txt", remote_path="/research/")
    print("[1] uploaded 3 files via mirror upload service")

    report = await scan_vault(_VAULT)
    assert report.in_sync_count == 3, \
        f"expected 3 in_sync, got {report.in_sync_count}"
    assert report.total_changes == 0
    print(f"[2] initial scan: in_sync={report.in_sync_count}, changes=0")

    # 3. Drop a file on disk.
    new_disk = _VAULT / "research" / "delta.txt"
    new_disk.write_bytes(b"externally added\n")
    # 4. Rename a file on disk.
    old_alpha = _VAULT / "notes" / "alpha.txt"
    new_alpha = _VAULT / "notes" / "alpha-renamed.txt"
    os.rename(old_alpha, new_alpha)
    # 5. Delete a file.
    (_VAULT / "notes" / "beta.txt").unlink()
    # 6. Edit a file in place (same path, different bytes).
    edited_path = _VAULT / "research" / "gamma.txt"
    edited_path.write_bytes(b"third body, edited externally\n")

    report = await scan_vault(_VAULT)
    assert len(report.new) == 1 and report.new[0].name == "delta.txt", \
        f"expected 1 new (delta.txt), got {report.new}"
    assert len(report.moved) == 1 and report.moved[0][0].id == e1, \
        f"expected alpha moved, got {[(e.id, p) for e, p in report.moved]}"
    assert len(report.missing) == 1 and report.missing[0].id == e2, \
        f"expected beta missing, got {[e.id for e in report.missing]}"
    assert len(report.modified) == 1 and report.modified[0][0].id == e3, \
        f"expected gamma modified, got {[(e.id, p) for e, p in report.modified]}"
    print(f"[3-6] scan classified all four cases:")
    print(f"      new=1 (delta.txt) moved=1 (alpha→alpha-renamed) "
          f"missing=1 (beta) modified=1 (gamma)")

    # 7. Apply all in one go (this is what /ingest --all calls).
    out = await apply_all(report)
    print(f"[7] apply_all: {out}")
    assert out["ingested"] == 1
    assert out["moved"] == 1
    assert out["modified"] == 1
    assert out["forgotten"] == 1
    assert out["failures"] == [], f"unexpected failures: {out['failures']}"

    final = await scan_vault(_VAULT)
    assert final.total_changes == 0, \
        f"post-apply expected 0 changes, got {final.total_changes}: " \
        f"new={len(final.new)} missing={len(final.missing)} " \
        f"moved={len(final.moved)} modified={len(final.modified)}"
    assert final.in_sync_count == 3, \
        f"expected 3 in_sync, got {final.in_sync_count}"
    print(f"[8] final: in_sync={final.in_sync_count}, changes=0")

    # 9. Verify the gamma entry kept its identity through modify.
    factory = get_session_factory()
    async with factory() as s:
        e = await s.get(FileEntry, e3)
        assert e is not None and e.deleted_at is None, \
            "gamma entry should survive modify (identity stable)"
    print(f"[9] modify kept entry id stable (gamma entry={e3[:8]} still alive)")

    # 10. Cross-folder move: alpha-renamed → archive/old/.
    new_alpha2 = _VAULT / "archive" / "old" / "alpha-renamed.txt"
    new_alpha2.parent.mkdir(parents=True, exist_ok=True)
    os.rename(_VAULT / "notes" / "alpha-renamed.txt", new_alpha2)
    cross = await scan_vault(_VAULT)
    assert len(cross.moved) == 1, \
        f"expected 1 cross-folder moved, got {[(e.display_name, p) for e, p in cross.moved]}"
    n_moved2, moved_failures = await apply_moved(cross)
    assert moved_failures == [], f"unexpected failures: {moved_failures}"
    assert n_moved2 == 1, f"expected 1 cross-folder move applied, got {n_moved2}"
    after_cross = await scan_vault(_VAULT)
    assert after_cross.total_changes == 0
    print(f"[10] cross-folder move applied; auto-created /archive/old")

    print("\nALL SCAN_SYNC E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
