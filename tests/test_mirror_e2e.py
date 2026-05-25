"""Mirror backend e2e — verify the human-friendly storage layout.

Run:
    .venv/Scripts/python tests/test_mirror_e2e.py

Verifies the contract that makes mirror useful:

  1. Upload → file appears at <vault>/<folder>/<sanitized-name> on disk.
  2. Same sha256 uploaded twice → two real files on disk (dedup OFF in
     mirror), two file rows in db.
  3. Filename collision in same folder → second upload gets ' (2)' suffix
     both in db display_name AND on-disk filename.
  4. Illegal characters get sanitized: 'Q3 report: draft.pdf' →
     'Q3 report_ draft.pdf' on disk (Linux portable to Windows).
  5. Reserved name CON.txt → CON_.txt on disk (Windows reserved-name
     handling stays portable).
  6. Soft delete → disk file untouched (purge will clean it; that's
     out of scope for this test).

We exercise the upload service directly to skip the HTTP layer and
keep the fixtures small.
"""
from __future__ import annotations

import asyncio
import io
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_mirror_e2e_data"
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

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File, FileEntry  # noqa: E402
from marginalia.storage import (  # noqa: E402
    MirrorStorage, get_storage, reset_storage_cache,
)


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _upload(body: bytes, *, name: str, remote_path: str) -> dict:
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
        return {
            "file_id": result.file_id,
            "entry_id": result.entry_id,
            "display_name": result.display_name,
            "deduped": result.deduped,
            "auto_renamed": result.auto_renamed,
        }


# ---- test ------------------------------------------------------------------

async def _main() -> None:
    reset_storage_cache()
    await _create_schema()

    storage = get_storage()
    assert isinstance(storage, MirrorStorage), \
        f"expected MirrorStorage, got {type(storage).__name__}"
    print(f"[setup] mirror storage rooted at {_VAULT}")

    # 1. Plain upload → folder-tree on disk.
    body_a = b"alpha beta gamma\n"
    r1 = await _upload(body_a, name="notes.txt", remote_path="/research/llm/")
    expected = _VAULT / "research" / "llm" / "notes.txt"
    assert expected.is_file(), f"expected file at {expected}, listing:" \
        + str(list(_VAULT.rglob("*")))
    assert expected.read_bytes() == body_a
    print(f"[1] file landed at vault/research/llm/notes.txt")

    # 2. Same content, different folder → two real files (dedup off).
    r2 = await _upload(body_a, name="notes.txt", remote_path="/copies/")
    expected2 = _VAULT / "copies" / "notes.txt"
    assert expected2.is_file(), f"missing {expected2}"
    assert r1["file_id"] != r2["file_id"], \
        "dedup must be OFF in mirror — got same file_id"
    factory = get_session_factory()
    async with factory() as s:
        from sqlalchemy import select
        rows = (await s.execute(
            select(File).where(File.sha256 ==
                (await s.get(File, r1["file_id"])).sha256)
        )).scalars().all()
        assert len(rows) == 2, f"expected 2 file rows, got {len(rows)}"
    print(f"[2] dedup off: same sha256 → 2 file rows + 2 disk files")

    # 3. Collision in same folder → ' (2)' suffix.
    body_b = b"different content\n"
    r3 = await _upload(body_b, name="notes.txt", remote_path="/research/llm/")
    second = _VAULT / "research" / "llm" / "notes (2).txt"
    assert second.is_file(), \
        f"expected ' (2)' rename on disk, got listing: " + \
        str(list((_VAULT / "research" / "llm").iterdir()))
    assert second.read_bytes() == body_b
    print(f"[3] collision rename: second notes.txt → notes (2).txt")

    # 4. Illegal char in display_name → sanitize on disk.
    r4 = await _upload(
        b"q3 body\n",
        name="Q3 report: draft.pdf",
        remote_path="/reports/2026/",
    )
    sanitized = _VAULT / "reports" / "2026" / "Q3 report_ draft.pdf"
    assert sanitized.is_file(), \
        f"expected sanitized filename {sanitized.name}, listing: " + \
        str(list((_VAULT / "reports" / "2026").iterdir()))
    print(f"[4] sanitize: 'Q3 report: draft.pdf' → 'Q3 report_ draft.pdf'")

    # 5. Reserved name handling.
    r5 = await _upload(b"reserved\n", name="CON.txt", remote_path="/odd/")
    reserved = _VAULT / "odd" / "CON_.txt"
    assert reserved.is_file(), \
        f"expected CON_.txt, listing: " + \
        str(list((_VAULT / "odd").iterdir()))
    print(f"[5] reserved: 'CON.txt' → 'CON_.txt'")

    # 6. Soft delete keeps disk file alive.
    from marginalia.services.entries import soft_delete_entry
    factory = get_session_factory()
    async with factory() as s:
        await soft_delete_entry(s, entry_id=r1["entry_id"])
        await s.commit()
    assert expected.is_file(), \
        "soft delete must NOT remove the disk file (purge does)"
    async with factory() as s:
        e = await s.get(FileEntry, r1["entry_id"])
        assert e.deleted_at is not None, "deleted_at should be set"
    print(f"[6] soft delete leaves disk file in place")

    # 7. Rename moves the on-disk file too.
    from marginalia.services.entries import rename_entry, move_entry
    async with factory() as s:
        await rename_entry(s, entry_id=r4["entry_id"],
                           new_name="Q3 final.pdf")
        await s.commit()
    renamed = _VAULT / "reports" / "2026" / "Q3 final.pdf"
    assert renamed.is_file(), \
        f"rename should have moved disk file; listing: " + \
        str(list((_VAULT / "reports" / "2026").iterdir()))
    assert not sanitized.is_file(), \
        f"old path {sanitized} still present after rename"
    print(f"[7] rename moves disk file: 'Q3 report_ draft.pdf' → 'Q3 final.pdf'")

    # 8. Move-to-different-folder relocates disk file.
    from marginalia.services.folders import resolve_or_create_folder
    async with factory() as s:
        new_folder = await resolve_or_create_folder(
            s, segments=["archive", "old"]
        )
        await s.commit()
        moved_entry = await s.get(FileEntry, r4["entry_id"])
        assert moved_entry is not None
    async with factory() as s:
        await move_entry(s, entry_id=r4["entry_id"],
                         new_folder_id=new_folder.id)
        await s.commit()
    moved_disk = _VAULT / "archive" / "old" / "Q3 final.pdf"
    assert moved_disk.is_file(), \
        f"move should have relocated disk file; listing: " + \
        str(list(_VAULT.rglob("*Q3*")))
    assert not renamed.is_file(), \
        f"old folder path {renamed} still present after move"
    print(f"[8] move relocates disk file across folders")

    print("\nALL MIRROR E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
