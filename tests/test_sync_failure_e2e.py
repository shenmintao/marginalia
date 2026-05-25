"""Failure-collection contract for sync.apply_*.

The /ingest --all command used to silently log per-item failures and
report success counts only — a vault scan with 50 broken files would
print `ingested=0 modified=0 moved=0 forgotten=0` with no signal.

This test forces a partial failure (modified file deleted between scan
and apply) and confirms:
  - apply_modified returns (n, [SyncFailure]) with the failure captured
  - apply_all surfaces failures via its dict
  - successful items in the same batch still commit

Run:
    .venv/Scripts/python tests/test_sync_failure_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_sync_failure_e2e_data"
_VAULT = _TEST_ROOT / "library"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "mirror"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base  # noqa: E402
from marginalia.services.scan import scan_vault  # noqa: E402
from marginalia.services.sync import (  # noqa: E402
    SyncFailure, apply_all, apply_modified,
)
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

    a_id = await _upload(b"alpha body\n", name="a.txt", remote_path="notes/a.txt")
    b_id = await _upload(b"beta body\n", name="b.txt", remote_path="notes/b.txt")
    print(f"[1] uploaded a={a_id[:8]} b={b_id[:8]}")

    # Edit both so scan sees them as modified.
    (_VAULT / "notes" / "a.txt").write_bytes(b"alpha edited\n")
    (_VAULT / "notes" / "b.txt").write_bytes(b"beta edited\n")
    report = await scan_vault(_VAULT)
    assert len(report.modified) == 2, \
        f"expected 2 modified, got {len(report.modified)}"
    print(f"[2] scan saw {len(report.modified)} modified")

    # Now break one: delete b.txt before apply runs. apply_modified
    # opens the path to re-hash; FileNotFoundError must be captured as
    # a SyncFailure rather than aborting the whole batch.
    (_VAULT / "notes" / "b.txt").unlink()

    n, failures = await apply_modified(report)
    assert n == 1, f"the surviving file should still apply; got n={n}"
    assert len(failures) == 1, f"expected 1 failure, got {failures}"
    f0 = failures[0]
    assert isinstance(f0, SyncFailure)
    assert f0.category == "modified"
    assert "b.txt" in f0.target
    assert "FileNotFoundError" in f0.error or "Errno 2" in f0.error, \
        f"failure should carry a meaningful error; got {f0.error!r}"
    print(f"[3] apply_modified n={n} failures=[{f0.category}: "
          f"{f0.target} → {f0.error.split(':')[0]}]")

    # apply_all also surfaces failures via the dict it returns.
    (_VAULT / "notes" / "a.txt").write_bytes(b"alpha edited again\n")
    (_VAULT / "notes" / "c.txt").write_bytes(b"new file\n")
    report2 = await scan_vault(_VAULT)
    out = await apply_all(report2)
    assert isinstance(out["failures"], list)
    print(f"[4] apply_all surfaces failures key: keys={sorted(out.keys())}")

    print("\nALL SYNC FAILURE E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
