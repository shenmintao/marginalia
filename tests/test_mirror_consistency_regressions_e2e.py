"""Regression tests for the mirror-consistency fixes from
audit-report-2026-07-02.md (services/storage side).

Covered fixes:

  1. GUI upload with folder_id lands in the folder's vault directory,
     not the vault root (bugs #5 / #11, services/upload.py).
  2. Name-collision suffix is the same string on disk and in
     display_name — both ' (1)' (bugs #39 / #55, storage/mirror.py +
     services/upload.py).
  3. Moving a mirrored file to the vault root on disk can be applied:
     scan → apply_moved sets folder_id NULL + storage_key, and a second
     scan no longer flags the move (bug #20, services/sync.py). Also
     move_entry(new_folder_id=None) works directly (PATCH-level).
  4. Renaming / moving a non-hydrated WebDAV placeholder entry does not
     raise FileNotFoundError and never touches storage.rename
     (bugs #21 / #37, services/entries.py).
  5. Folder rename relocates every file in the subtree on disk and
     updates storage_keys; a scan reports the entries in_sync
     (bug #38, services/folders.py).
  6. Folder-download zip member paths are sanitized: a display_name
     containing '../' cannot produce traversal members
     (bug #76 zip side, services/user_files.py).
  7. scan_vault never claims another live entry's path as a mover for a
     deleted duplicate: the survivor stays in_sync, the deleted one is
     reported missing (bug #19, services/scan.py).

Run:
    .venv/bin/pytest tests/test_mirror_consistency_regressions_e2e.py -q
"""
from __future__ import annotations

import os
from uuid import uuid4
from datetime import datetime, timezone
from pathlib import Path

_TEST_PARENT = Path(os.environ.get("MARGINALIA_TEST_TMP", Path(__file__).resolve().parent))
_TEST_ROOT = _TEST_PARENT / f"_mirror_consistency_e2e_data_{os.getpid()}_{uuid4().hex[:8]}"
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
from marginalia.storage import MirrorStorage, get_storage  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402


# ---- helpers ---------------------------------------------------------------

async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _upload(
    body: bytes,
    *,
    name: str,
    remote_path: str | None = None,
    folder_id: str | None = None,
):
    from marginalia.services.upload import upload

    storage = get_storage()
    assert isinstance(storage, MirrorStorage)

    async def _stream():
        yield body

    factory = get_session_factory()
    async with factory() as db:
        result = await upload(
            db, storage,
            stream=_stream(), fallback_name=name,
            remote_path=remote_path, folder_id=folder_id,
            content_type="text/plain",
        )
        await db.commit()
        return result


async def _make_folder(segments: list[str]) -> str:
    from marginalia.services.folders import resolve_or_create_folder

    factory = get_session_factory()
    async with factory() as s:
        folder = await resolve_or_create_folder(s, segments=segments)
        await s.commit()
        assert folder is not None
        return folder.id


async def _seed_webdav_placeholder(
    *, folder_id: str | None, display_name: str,
) -> tuple[str, str]:
    """Insert a non-hydrated WebDAV placeholder (bug #21/#37 shape):
    File.storage_key = '_webdav/<id>' with nothing on disk."""
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        fid = new_id()
        file_row = File(
            id=fid, storage_key=f"_webdav/{fid}", sha256=uuid4().hex * 2,
            size_bytes=42, mime_type="text/plain",
            original_ext=".txt", kind="text",
            summary=None,
            description={"_webdav_remote": {
                "hydrated": False,
                "remote_path": f"/dav/{display_name}",
                "etag": "etag-1",
            }},
            extra=None, ingest_status="done", ingested_at=now,
            created_at=now, updated_at=now,
        )
        s.add(file_row)
        await s.flush()
        entry = FileEntry(
            id=new_id(), folder_id=folder_id, file_id=fid,
            display_name=display_name, lifecycle="active",
            catalog_id=None, extra=None, created_at=now, updated_at=now,
        )
        s.add(entry)
        await s.commit()
        return entry.id, fid


def _diff_entry_ids(report) -> dict[str, set[str]]:
    return {
        "moved": {e.id for e, _p in report.moved},
        "missing": {e.id for e in report.missing},
        "modified": {e.id for e, _p in report.modified},
    }


# ---- 1. GUI upload (folder_id) lands inside the folder dir (bugs #5/#11) --

async def test_gui_upload_with_folder_id_lands_in_folder_directory() -> None:
    folder_id = await _make_folder(["projects", "alpha"])
    body = b"gui upload body\n"
    r = await _upload(body, name="report.txt", folder_id=folder_id)

    in_folder = _VAULT / "projects" / "alpha" / "report.txt"
    assert in_folder.is_file(), (
        f"folder_id upload must land under the folder dir; vault: "
        f"{[str(p) for p in _VAULT.rglob('*') if p.is_file()]}"
    )
    assert in_folder.read_bytes() == body
    assert not (_VAULT / "report.txt").exists(), \
        "folder_id upload leaked to the vault root (old bug #5/#11)"
    assert r.folder_id == folder_id

    factory = get_session_factory()
    async with factory() as s:
        file_row = await s.get(File, r.file_id)
        assert file_row.storage_key == "projects/alpha/report.txt"


# ---- 2. Collision numbering: disk basename == display_name (bugs #39/#55) -

async def test_collision_suffix_identical_on_disk_and_in_db() -> None:
    body1 = b"first collide body\n"
    body2 = b"second collide body\n"
    await _upload(body1, name="notes.txt", remote_path="/collide/")
    r2 = await _upload(body2, name="notes.txt", remote_path="/collide/")

    assert r2.display_name == "notes (1).txt", \
        f"expected DB display_name 'notes (1).txt', got {r2.display_name!r}"
    assert r2.auto_renamed is True

    factory = get_session_factory()
    async with factory() as s:
        file_row = await s.get(File, r2.file_id)
    disk_basename = file_row.storage_key.rsplit("/", 1)[-1]
    assert disk_basename == r2.display_name, (
        f"disk basename {disk_basename!r} diverged from display_name "
        f"{r2.display_name!r} (old bug: ' (2)' vs ' (1)')"
    )
    on_disk = _VAULT / "collide" / "notes (1).txt"
    assert on_disk.is_file(), (
        f"expected 'notes (1).txt' on disk; listing: "
        f"{[p.name for p in (_VAULT / 'collide').iterdir()]}"
    )
    assert on_disk.read_bytes() == body2


# ---- 3a. PATCH-level: move_entry(new_folder_id=None) → vault root (bug #20)

async def test_move_entry_to_root_via_service() -> None:
    from marginalia.services.entries import move_entry

    r = await _upload(b"patch move body\n", name="patchfile.txt",
                      remote_path="/patchsrc/")
    factory = get_session_factory()
    async with factory() as s:
        moved = await move_entry(s, entry_id=r.entry_id, new_folder_id=None)
        await s.commit()
        assert moved.folder_id is None

    assert (_VAULT / "patchfile.txt").is_file(), \
        "move to root must relocate the disk file to the vault root"
    assert not (_VAULT / "patchsrc" / "patchfile.txt").exists()
    async with factory() as s:
        file_row = await s.get(File, r.file_id)
        assert file_row.storage_key == "patchfile.txt"


# ---- 3b. Disk-side move to root: scan + apply_moved converge (bug #20) ----

async def test_scan_apply_moved_handles_move_to_vault_root() -> None:
    from marginalia.services.scan import scan_vault
    from marginalia.services.sync import apply_moved

    r = await _upload(b"root move body\n", name="rootmove.txt",
                      remote_path="/movesrc/")
    # user moves the file to the vault root outside the app
    os.replace(_VAULT / "movesrc" / "rootmove.txt", _VAULT / "rootmove.txt")

    report1 = await scan_vault(_VAULT)
    moved_mine = [(e, p) for e, p in report1.moved if e.id == r.entry_id]
    assert moved_mine, "scan must detect the root-ward move"
    assert moved_mine[0][1] == _VAULT / "rootmove.txt"

    report1.moved = moved_mine  # apply only our diff
    n, failures = await apply_moved(report1)
    assert failures == [], f"apply_moved failed: {failures}"
    assert n == 1, "apply_moved must apply the move to root (old: no-op)"

    factory = get_session_factory()
    async with factory() as s:
        entry = await s.get(FileEntry, r.entry_id)
        file_row = await s.get(File, r.file_id)
        assert entry.folder_id is None, \
            "entry must now live at the vault root (folder_id NULL)"
        assert file_row.storage_key == "rootmove.txt"

    report2 = await scan_vault(_VAULT)
    ids2 = _diff_entry_ids(report2)
    assert r.entry_id not in ids2["moved"], \
        "second scan still flags the same move (old bug: flagged forever)"
    assert r.entry_id not in ids2["missing"]
    assert r.entry_id not in ids2["modified"]
    assert (_VAULT / "rootmove.txt") not in report2.new


# ---- 4. Non-hydrated WebDAV placeholder rename/move (bugs #21/#37) --------

async def test_non_hydrated_webdav_entry_rename_and_move() -> None:
    from marginalia.services.entries import move_entry, rename_entry

    folder_id = await _make_folder(["dav"])
    entry_id, file_id = await _seed_webdav_placeholder(
        folder_id=folder_id, display_name="remote-doc.txt",
    )

    factory = get_session_factory()
    # rename: must not raise FileNotFoundError (no file on disk)
    async with factory() as s:
        entry = await rename_entry(s, entry_id=entry_id,
                                   new_name="remote-renamed.txt")
        await s.commit()
        assert entry.display_name == "remote-renamed.txt"

    # move to root: same guard applies
    async with factory() as s:
        entry = await move_entry(s, entry_id=entry_id, new_folder_id=None)
        await s.commit()
        assert entry.folder_id is None

    async with factory() as s:
        file_row = await s.get(File, file_id)
        assert file_row.storage_key == f"_webdav/{file_id}", \
            "placeholder storage_key must be left alone (no storage.rename)"
    assert not (_VAULT / "_webdav").exists(), \
        "storage.rename must not have been attempted for the placeholder"


# ---- 5. Folder rename relocates the vault directory (bug #38) -------------

async def test_folder_rename_relocates_mirror_vault_directory() -> None:
    from marginalia.services.folders import rename_folder
    from marginalia.services.scan import scan_vault

    team_id = await _make_folder(["team"])
    await _make_folder(["team", "sub"])
    ra = await _upload(b"team file a\n", name="a.txt", remote_path="/team/")
    rb = await _upload(b"team file b\n", name="b.txt",
                       remote_path="/team/sub/")

    factory = get_session_factory()
    async with factory() as s:
        await rename_folder(s, folder_id=team_id, new_name="team-renamed")
        await s.commit()

    new_a = _VAULT / "team-renamed" / "a.txt"
    new_b = _VAULT / "team-renamed" / "sub" / "b.txt"
    assert new_a.is_file() and new_b.is_file(), (
        f"files must follow the renamed folder; vault: "
        f"{[str(p.relative_to(_VAULT)) for p in _VAULT.rglob('*') if p.is_file()]}"
    )
    old_dir = _VAULT / "team"
    assert not old_dir.exists() or not any(
        p.is_file() for p in old_dir.rglob("*")
    ), "old vault directory still holds files after folder rename"

    async with factory() as s:
        fa = await s.get(File, ra.file_id)
        fb = await s.get(File, rb.file_id)
        assert fa.storage_key == "team-renamed/a.txt"
        assert fb.storage_key == "team-renamed/sub/b.txt"

    report = await scan_vault(_VAULT)
    ids = _diff_entry_ids(report)
    for eid in (ra.entry_id, rb.entry_id):
        assert eid not in ids["moved"], \
            "scan flags renamed-folder files as moved (disk not relocated?)"
        assert eid not in ids["missing"]
        assert eid not in ids["modified"]
    new_paths = set(report.new)
    assert new_a not in new_paths and new_b not in new_paths


# ---- 6. Zip member sanitization (bug #76, zip side) ------------------------

async def test_folder_zip_members_neutralize_traversal_display_name() -> None:
    from marginalia.services.user_files import collect_folder_entries

    folder_id = await _make_folder(["zips"])
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        fid = new_id()
        s.add(File(
            id=fid, storage_key="zips/whatever.txt", sha256="e" * 64,
            size_bytes=5, mime_type="text/plain",
            original_ext=".txt", kind="text",
            summary=None, description=None, extra=None,
            ingest_status="done", ingested_at=now,
            created_at=now, updated_at=now,
        ))
        await s.flush()
        # hostile display_name (e.g. via WebDAV metadata import)
        s.add(FileEntry(
            id=new_id(), folder_id=folder_id, file_id=fid,
            display_name="../../secret.txt", lifecycle="active",
            catalog_id=None, extra=None, created_at=now, updated_at=now,
        ))
        await s.commit()

    async with factory() as s:
        members = await collect_folder_entries(s, folder_id=folder_id)
    assert members, "expected the hostile entry to still be a zip member"
    for zip_path, _entry, _file in members:
        assert not zip_path.startswith("/"), zip_path
        assert ".." not in zip_path.split("/"), (
            f"zip member path {zip_path!r} contains a traversal segment "
            f"(old bug: display_name used verbatim)"
        )
    assert any("secret" in zp for zp, _e, _f in members), \
        "sanitized member should preserve a recognizable name"


# ---- 7. Scan mover ownership with duplicate sha256 (bug #19) ---------------
# (last on purpose: it leaves a missing entry behind)

async def test_scan_does_not_claim_siblings_path_for_deleted_duplicate() -> None:
    from marginalia.services.scan import scan_vault

    body = b"identical duplicate body\n"
    ra = await _upload(body, name="dupA.txt", remote_path="/dup/")
    rb = await _upload(body, name="dupB.txt", remote_path="/dup/")
    assert ra.file_id != rb.file_id, "mirror mode must keep dedup off"

    # user deletes A's file on disk; B's identical file must keep its path
    (_VAULT / "dup" / "dupA.txt").unlink()

    report = await scan_vault(_VAULT)
    ids = _diff_entry_ids(report)
    assert ra.entry_id in ids["missing"], \
        "deleted duplicate must be reported missing"
    assert ra.entry_id not in ids["moved"], \
        "scan claimed the sibling's file as A's move (old bug #19)"
    moved_targets = {p for _e, p in report.moved}
    assert (_VAULT / "dup" / "dupB.txt") not in moved_targets, \
        "no entry may claim B's path as a move target"
    assert rb.entry_id not in ids["moved"]
    assert rb.entry_id not in ids["missing"]
    assert rb.entry_id not in ids["modified"]
