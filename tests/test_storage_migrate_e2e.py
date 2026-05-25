"""Storage migrate e2e — local → mirror → local round-trip.

Run:
    .venv/Scripts/python tests/test_storage_migrate_e2e.py

Verifies that:
  1. With STORAGE_BACKEND=local, upload N files. They land at UUID-flat
     paths under <home>/objects/.
  2. Run `_run_migrate(local → mirror)`. Files land at <home>/library/
     under sanitized folder + display_name. Reads through the mirror
     storage instance return the same bytes.
  3. files.storage_key in db now matches mirror shape (no UUID).
  4. Run the reverse migration (mirror → local). Files land back as
     UUID-flat under <home>/objects/. Bytes still match.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_storage_migrate_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)

# Start with local backend; we'll swap mid-test.
_LIBRARY = _TEST_ROOT / "library"
_OBJECTS = _TEST_ROOT / "objects"
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File  # noqa: E402
from marginalia.storage import get_storage, reset_storage_cache  # noqa: E402


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
        }


async def _main() -> None:
    reset_storage_cache()
    await _create_schema()

    # Phase 1: upload to local backend.
    bodies = [
        (b"first body\n", "first.txt", "/research/llm/"),
        (b"second body\n", "Q3 report.pdf", "/reports/2026/"),
        (b"third body\n", "notes.md", "/notes/"),
    ]
    file_ids: list[str] = []
    for body, name, path in bodies:
        r = await _upload(body, name=name, remote_path=path)
        file_ids.append(r["file_id"])
    print(f"[1] uploaded {len(file_ids)} files via local backend")

    # Verify they're at UUID-flat paths.
    factory = get_session_factory()
    async with factory() as s:
        for fid in file_ids:
            f = await s.get(File, fid)
            assert "/" in f.storage_key and len(f.storage_key) > 30, \
                f"expected UUID-flat key, got {f.storage_key!r}"
    print(f"[2] all 3 files have UUID-flat keys, e.g. "
          f"{(await _peek_first_key()).split('/')[-1][:8]}…")

    # Phase 2: run migrate local → mirror.
    from marginalia.cli.storage_cmd import _run_migrate
    rc = await _run_migrate("local", "mirror", dry_run=False)
    assert rc == 0, f"migrate exit code {rc}"
    print("[3] migrate local → mirror returned 0")

    # Verify all files now have mirror-shape keys + content unchanged.
    async with factory() as s:
        for fid, (body, _, _) in zip(file_ids, bodies):
            f = await s.get(File, fid)
            assert ".pdf" in f.storage_key or ".txt" in f.storage_key \
                or ".md" in f.storage_key, \
                f"expected mirror-shape key with extension, got {f.storage_key!r}"
            disk = _LIBRARY / f.storage_key
            assert disk.is_file(), f"missing disk file at {disk}"
            assert disk.read_bytes() == body, \
                f"content mismatch at {disk}: {disk.read_bytes()[:20]!r} vs {body[:20]!r}"
    # Old objects/ dir should be empty (or near-empty).
    remaining = list(_OBJECTS.rglob("*")) if _OBJECTS.exists() else []
    remaining_files = [p for p in remaining if p.is_file()]
    assert len(remaining_files) == 0, \
        f"objects/ still has files after migrate: {remaining_files}"
    print(f"[4] mirror layout: {len(file_ids)} files, content matches, "
          f"objects/ cleaned")

    # Phase 3: reverse migration mirror → local.
    os.environ["STORAGE_BACKEND"] = "mirror"
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage_cache()
    rc = await _run_migrate("mirror", "local", dry_run=False)
    assert rc == 0, f"reverse migrate exit code {rc}"

    async with factory() as s:
        for fid, (body, _, _) in zip(file_ids, bodies):
            f = await s.get(File, fid)
            assert "/" in f.storage_key and "." not in f.storage_key.split("/")[-1], \
                f"expected UUID-flat after reverse, got {f.storage_key!r}"
            disk = _OBJECTS / f.storage_key
            assert disk.is_file(), f"missing {disk}"
            assert disk.read_bytes() == body
    leftover = [p for p in _LIBRARY.rglob("*") if p.is_file()] \
        if _LIBRARY.exists() else []
    assert len(leftover) == 0, \
        f"library/ has stragglers after reverse migrate: {leftover}"
    print(f"[5] reverse migrate ok; back to UUID-flat, library/ cleaned")

    print("\nALL STORAGE_MIGRATE E2E CHECKS PASSED")


async def _peek_first_key() -> str:
    factory = get_session_factory()
    async with factory() as s:
        from sqlalchemy import select
        return (await s.execute(select(File.storage_key).limit(1))).scalar_one()


if __name__ == "__main__":
    asyncio.run(_main())
