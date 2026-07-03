"""Regression tests for the WebDAV sync conflict-guard / re-hydration fixes.

Covers audit-report-2026-07-02 bugs:
- #3/#10  changed remote bytes must invalidate the hydrated marker and be
          re-downloaded; already_local results must not count as downloads
- #13/#18 pull must not clobber newer local edits, wipe local-only tags,
          or resurrect newer local soft-deletes (conflicts are counted)
- #4/#22  folder import: parents-before-children ordering (self-FK) and
          (parent_id, name) reconciliation with folder_id remapping
- #12/#76 hostile manifests: path-shaped file ids are rejected and
          display_name is sanitized on import
- #35     full publish refuses a foreign remote library_id; after a pull
          (adopting the id) it read-and-merges remote-only rows

Run:
    .venv/bin/pytest tests/test_webdav_conflict_guard_e2e.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

_TEST_PARENT = Path(os.environ.get(
    "MARGINALIA_TEST_TMP",
    str(Path(__file__).resolve().parent),
))
_TEST_ROOT = _TEST_PARENT / f"_webdav_conflict_guard_e2e_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True, exist_ok=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"
os.environ["WEBDAV_URL"] = "https://dav.test"
os.environ["WEBDAV_REMOTE_PATH"] = "/marginalia-test"

from marginalia.config import Settings as _Settings  # noqa: E402

_Settings.model_config["env_file"] = None

from marginalia.config import get_settings  # noqa: E402
from marginalia.db.engine import dispose_engine, get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import (  # noqa: E402
    Base,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Tag,
)
from marginalia.services.user_files import get_user_metadata  # noqa: E402
from marginalia.services.webdav_sync import (  # noqa: E402
    WebDavConfigError,
    _adopt_library_id,
    _parse_jsonl,
    download_selected,
    hydrate_entry,
    publish_snapshot,
    pull_latest_metadata,
)
from marginalia.storage import get_storage, reset_storage_cache  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402

_REMOTE_ROOT = "/marginalia-test"


async def _create_schema() -> None:
    await _activate_home(_TEST_ROOT)


async def _activate_home(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.environ["MARGINALIA_HOME"] = str(path)
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["WORKER_ENABLED"] = "false"
    os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
    os.environ["LLM_DEFAULT_MODEL"] = "fake-model"
    os.environ["WEBDAV_URL"] = "https://dav.test"
    os.environ["WEBDAV_REMOTE_PATH"] = "/marginalia-test"
    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage_cache()
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _one_chunk(body: bytes) -> AsyncIterator[bytes]:
    yield body


async def _read_storage(key: str) -> bytes:
    out = bytearray()
    async for chunk in get_storage().get(key):
        out.extend(chunk)
    return bytes(out)


def _jsonl_test_bytes(rows: list[dict[str, object]]) -> bytes:
    if not rows:
        return b""
    return (
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        + "\n"
    ).encode("utf-8")


def _snapshot_root(snapshot_id: str) -> str:
    return f"{_REMOTE_ROOT}/snapshots/{snapshot_id}"


class _MemoryWebDavClient:
    remote: dict[str, bytes] = {}

    def __init__(self, _settings) -> None:
        pass

    async def aclose(self) -> None:
        return None

    async def read_json(self, path: str) -> dict | None:
        body = self.remote.get(path)
        return json.loads(body.decode("utf-8")) if body is not None else None

    async def read_bytes(self, path: str) -> bytes:
        return self.remote[path]

    async def exists(self, path: str) -> bool:
        return path in self.remote

    async def ensure_dir(self, path: str) -> None:
        return None

    async def put_bytes(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        self.remote[path] = body

    async def put_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str | None,
    ) -> None:
        body = bytearray()
        async for chunk in stream:
            body.extend(chunk)
        self.remote[path] = bytes(body)

    async def stream_to_storage(
        self,
        path: str,
        *,
        storage_key: str,
        display_name: str,
        folder_path: str | None,
        content_type: str | None,
        expected_sha256: str | None = None,
    ) -> str:
        body = self.remote[path]
        if expected_sha256:
            actual = hashlib.sha256(body).hexdigest()
            if actual != expected_sha256.lower():
                raise WebDavConfigError("downloaded blob sha256 mismatch")
        return await get_storage().put(
            storage_key,
            _one_chunk(body),
            content_type=content_type,
            display_name=display_name,
            folder_path=folder_path,
        )


def _use_memory_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _MemoryWebDavClient.remote = {}
    monkeypatch.setattr(
        "marginalia.services.webdav_sync.WebDavClient",
        _MemoryWebDavClient,
    )


async def _seed_entry(
    *,
    display_name: str,
    body: bytes,
    folder_id: str | None = None,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    file_id = new_id()
    entry_id = new_id()
    storage_key = f"seed/{file_id}"
    sha = hashlib.sha256(body).hexdigest()
    await get_storage().put(storage_key, _one_chunk(body), content_type="text/plain")
    factory = get_session_factory()
    async with factory() as session:
        session.add(File(
            id=file_id,
            storage_key=storage_key,
            sha256=sha,
            size_bytes=len(body),
            mime_type="text/plain",
            original_ext=".txt",
            kind="text",
            ingest_status="done",
            ingested_at=now,
            created_at=now,
            updated_at=now,
        ))
        await session.flush()
        session.add(FileEntry(
            id=entry_id,
            folder_id=folder_id,
            file_id=file_id,
            display_name=display_name,
            lifecycle="active",
            catalog_id=None,
            created_at=now,
            updated_at=now,
        ))
        await session.commit()
    return {"entry_id": entry_id, "file_id": file_id, "sha256": sha}


async def _update_entry_bytes(entry_id: str, body: bytes) -> str:
    sha = hashlib.sha256(body).hexdigest()
    factory = get_session_factory()
    async with factory() as session:
        entry = await session.get(FileEntry, entry_id)
        file_row = await session.get(File, entry.file_id)
        await get_storage().put(
            file_row.storage_key,
            _one_chunk(body),
            content_type="text/plain",
        )
        file_row.sha256 = sha
        file_row.size_bytes = len(body)
        file_row.updated_at = datetime.now(timezone.utc)
        await session.commit()
    return sha


async def _attach_local_tag(entry_id: str, name: str) -> str:
    now = datetime.now(timezone.utc)
    tag_id = new_id()
    factory = get_session_factory()
    async with factory() as session:
        session.add(Tag(
            id=tag_id,
            name=name,
            facet="topic",
            alias_of=None,
            doc_count=1,
            last_used_at=now,
            created_at=now,
            updated_at=now,
        ))
        await session.flush()
        session.add(EntryTag(
            entry_id=entry_id,
            tag_id=tag_id,
            source="ingest",
            created_at=now,
        ))
        await session.commit()
    return tag_id


async def _entry_tag_ids(entry_id: str) -> set[str]:
    factory = get_session_factory()
    async with factory() as session:
        return {
            str(tag_id)
            for tag_id in (
                await session.execute(
                    select(EntryTag.tag_id).where(EntryTag.entry_id == entry_id)
                )
            ).scalars()
        }


# ---------------------------------------------------------------------------
# 1. Changed-file re-hydration (bugs #3/#10)
# ---------------------------------------------------------------------------


async def test_changed_remote_bytes_invalidate_marker_and_redownload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "rehydrate_a"
    home_b = _TEST_ROOT / "rehydrate_b"
    _use_memory_client(monkeypatch)
    body_v1 = b"first revision of the document\n" * 4
    body_v2 = b"second revision with brand new bytes\n" * 4

    await _activate_home(home_a)
    seeded = await _seed_entry(display_name="doc.txt", body=body_v1)
    entry_id = seeded["entry_id"]
    await publish_snapshot()

    await _activate_home(home_b)
    first = await download_selected([entry_id])
    assert first["downloaded_files"] == 1
    assert first["failed_files"] == 0

    await _activate_home(home_a)
    sha_v2 = await _update_entry_bytes(entry_id, body_v2)
    await publish_snapshot()

    await _activate_home(home_b)
    await pull_latest_metadata()
    factory = get_session_factory()
    async with factory() as session:
        meta = await get_user_metadata(session, entry_id=entry_id)
        # Old behavior: the stale local blob was re-marked hydrated=True.
        assert meta["webdav_remote"]["hydrated"] is False

    hydrated = await hydrate_entry(entry_id)
    assert hydrated["hydrated"] is True
    assert not hydrated.get("already_local")

    async with factory() as session:
        entry = await session.get(FileEntry, entry_id)
        file_row = await session.get(File, entry.file_id)
        stored = await _read_storage(file_row.storage_key)
        assert stored == body_v2
        assert file_row.sha256 == sha_v2
        assert hashlib.sha256(stored).hexdigest() == file_row.sha256

    # Everything is local now: already_local results must not be counted.
    again = await download_selected([entry_id])
    assert again["failed_files"] == 0
    assert again["downloaded_files"] == 0


async def test_pull_then_download_selected_fetches_changed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit pull followed by download_selected (which re-pulls) must
    still replace stale bytes with the new remote revision."""
    home_a = _TEST_ROOT / "doublepull_a"
    home_b = _TEST_ROOT / "doublepull_b"
    _use_memory_client(monkeypatch)
    body_v1 = b"double pull revision one\n" * 4
    body_v2 = b"double pull revision two -- changed\n" * 4

    await _activate_home(home_a)
    seeded = await _seed_entry(display_name="doc.txt", body=body_v1)
    entry_id = seeded["entry_id"]
    await publish_snapshot()

    await _activate_home(home_b)
    assert (await download_selected([entry_id]))["downloaded_files"] == 1

    await _activate_home(home_a)
    await _update_entry_bytes(entry_id, body_v2)
    await publish_snapshot()

    await _activate_home(home_b)
    await pull_latest_metadata()
    result = await download_selected([entry_id])
    assert result["failed_files"] == 0
    assert result["downloaded_files"] == 1

    factory = get_session_factory()
    async with factory() as session:
        entry = await session.get(FileEntry, entry_id)
        file_row = await session.get(File, entry.file_id)
        stored = await _read_storage(file_row.storage_key)
        assert stored == body_v2
        assert hashlib.sha256(stored).hexdigest() == file_row.sha256


# ---------------------------------------------------------------------------
# 2. Conflict guard on pull (bugs #13/#18)
# ---------------------------------------------------------------------------


async def test_pull_preserves_newer_local_edits_and_merges_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "conflict_a"
    home_b = _TEST_ROOT / "conflict_b"
    _use_memory_client(monkeypatch)

    await _activate_home(home_a)
    e1 = await _seed_entry(display_name="source-one.txt", body=b"entry one body\n")
    e2 = await _seed_entry(display_name="source-two.txt", body=b"entry two body\n")
    pub = await publish_snapshot()
    snap_root = _snapshot_root(str(pub["snapshot_id"]))

    await _activate_home(home_b)
    await pull_latest_metadata()

    # Local edits on B, newer than the remote rows.
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    factory = get_session_factory()
    async with factory() as session:
        entry1 = await session.get(FileEntry, e1["entry_id"])
        entry1.display_name = "Renamed locally.txt"
        entry1.updated_at = future
        await session.commit()
    b_local = await _attach_local_tag(e1["entry_id"], "b-local")
    b_keep = await _attach_local_tag(e2["entry_id"], "b-keep")

    # The remote snapshot gains a tag on entry two (as if published elsewhere).
    a_remote = new_id()
    now_iso = datetime.now(timezone.utc).isoformat()
    tag_rows = _parse_jsonl(_MemoryWebDavClient.remote[f"{snap_root}/tags.jsonl"])
    tag_rows.append({
        "tag_id": a_remote,
        "name": "a-remote",
        "facet": "topic",
        "alias_of": None,
        "doc_count": 1,
        "last_used_at": now_iso,
        "last_reaffirmed_at": None,
        "reaffirm_count": 0,
        "created_at": now_iso,
        "updated_at": now_iso,
    })
    _MemoryWebDavClient.remote[f"{snap_root}/tags.jsonl"] = _jsonl_test_bytes(tag_rows)
    entry_rows = _parse_jsonl(_MemoryWebDavClient.remote[f"{snap_root}/entries.jsonl"])
    for row in entry_rows:
        if row["entry_id"] == e2["entry_id"]:
            row["tags"] = [{
                "tag_id": a_remote,
                "source": "ingest",
                "created_at": now_iso,
                "last_reaffirmed_at": None,
                "reaffirm_count": 0,
            }]
    _MemoryWebDavClient.remote[f"{snap_root}/entries.jsonl"] = _jsonl_test_bytes(entry_rows)

    pulled = await pull_latest_metadata()
    assert pulled["conflicts"] == 1

    async with factory() as session:
        entry1 = await session.get(FileEntry, e1["entry_id"])
        # Old behavior: unconditional remote-wins reverted the rename.
        assert entry1.display_name == "Renamed locally.txt"
    # Old behavior: EntryTags were delete-and-replaced, wiping local tags.
    assert await _entry_tag_ids(e1["entry_id"]) == {b_local}
    assert await _entry_tag_ids(e2["entry_id"]) == {b_keep, a_remote}


async def test_pull_does_not_resurrect_newer_local_soft_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "softdelete_a"
    home_b = _TEST_ROOT / "softdelete_b"
    _use_memory_client(monkeypatch)

    await _activate_home(home_a)
    seeded = await _seed_entry(display_name="doomed.txt", body=b"delete me locally\n")
    entry_id = seeded["entry_id"]
    await publish_snapshot()

    await _activate_home(home_b)
    await pull_latest_metadata()

    deleted_at = datetime.now(timezone.utc) + timedelta(hours=1)
    purge_after = deleted_at + timedelta(days=30)
    factory = get_session_factory()
    async with factory() as session:
        entry = await session.get(FileEntry, entry_id)
        entry.deleted_at = deleted_at
        entry.purge_after = purge_after
        await session.commit()

    pulled = await pull_latest_metadata()
    assert pulled["conflicts"] == 1

    async with factory() as session:
        entry = await session.get(FileEntry, entry_id)
        # Old behavior: deleted_at/purge_after were unconditionally cleared.
        assert entry.deleted_at is not None
        assert entry.purge_after is not None


# ---------------------------------------------------------------------------
# 3. Folder ordering + (parent_id, name) reconciliation (bugs #4/#22)
# ---------------------------------------------------------------------------


async def test_child_exported_before_parent_imports_without_fk_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "folderorder_a"
    home_b = _TEST_ROOT / "folderorder_b"
    _use_memory_client(monkeypatch)

    await _activate_home(home_a)
    parent_id = new_id()
    child_id = new_id()
    parent_created = datetime(2026, 1, 2, tzinfo=timezone.utc)
    child_created = datetime(2025, 1, 2, tzinfo=timezone.utc)
    factory = get_session_factory()
    async with factory() as session:
        session.add(Folder(
            id=parent_id,
            parent_id=None,
            name="new-parent",
            created_at=parent_created,
            updated_at=parent_created,
        ))
        await session.flush()
        # Reparented later: older child now lives under the newer folder.
        session.add(Folder(
            id=child_id,
            parent_id=parent_id,
            name="old-child",
            created_at=child_created,
            updated_at=child_created,
        ))
        await session.commit()

    pub = await publish_snapshot()
    exported = _parse_jsonl(
        _MemoryWebDavClient.remote[f"{_snapshot_root(str(pub['snapshot_id']))}/folders.jsonl"]
    )
    # Export order is created_at asc, so the child precedes its parent.
    assert [row["folder_id"] for row in exported] == [child_id, parent_id]

    await _activate_home(home_b)
    pulled = await pull_latest_metadata()  # old behavior: FK IntegrityError
    assert pulled["folders"] == 2

    async with get_session_factory()() as session:
        parent = await session.get(Folder, parent_id)
        child = await session.get(Folder, child_id)
        assert parent is not None and parent.parent_id is None
        assert child is not None and child.parent_id == parent_id


async def test_same_name_root_folders_reconcile_and_remap_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "folderdupe_a"
    home_b = _TEST_ROOT / "folderdupe_b"
    _use_memory_client(monkeypatch)

    await _activate_home(home_a)
    now = datetime.now(timezone.utc)
    folder_a = new_id()
    factory = get_session_factory()
    async with factory() as session:
        session.add(Folder(
            id=folder_a,
            parent_id=None,
            name="Papers",
            created_at=now,
            updated_at=now,
        ))
        await session.commit()
    seeded = await _seed_entry(
        display_name="paper.txt",
        body=b"a paper\n",
        folder_id=folder_a,
    )
    await publish_snapshot()

    await _activate_home(home_b)
    folder_b = new_id()
    b_now = datetime.now(timezone.utc)
    factory = get_session_factory()
    async with factory() as session:
        session.add(Folder(
            id=folder_b,
            parent_id=None,
            name="Papers",
            created_at=b_now,
            updated_at=b_now,
        ))
        await session.commit()

    await pull_latest_metadata()

    async with factory() as session:
        papers = (
            await session.execute(
                select(Folder).where(
                    Folder.name == "Papers",
                    Folder.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        # Old behavior: two live root folders both named "Papers".
        assert [row.id for row in papers] == [folder_b]
        entry = await session.get(FileEntry, seeded["entry_id"])
        assert entry is not None
        assert entry.folder_id == folder_b


# ---------------------------------------------------------------------------
# 4. Hostile manifest neutralization (bugs #12/#76, import side)
# ---------------------------------------------------------------------------


async def test_hostile_manifest_ids_and_names_are_neutralized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _TEST_ROOT / "hostile"
    _use_memory_client(monkeypatch)
    await _activate_home(home)

    snapshot_id = "feedfacefeedface"
    snap_root = _snapshot_root(snapshot_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    sha = "ab" * 32
    traversal_entry_id = new_id()
    evil_entry_id = new_id()
    evil_file_id = new_id()

    def _file_meta(file_id: str) -> dict[str, object]:
        return {
            "file_id": file_id,
            "sha256": sha,
            "blob_path": f"blobs/sha256/ab/{sha}",
            "size_bytes": 3,
            "mime_type": "text/plain",
            "original_ext": ".txt",
            "kind": "text",
            "ingest_status": "done",
            "created_at": now_iso,
            "updated_at": now_iso,
        }

    entries = [
        {
            "entry_id": traversal_entry_id,
            "folder_id": None,
            "display_name": "innocuous.txt",
            "lifecycle": "active",
            "created_at": now_iso,
            "updated_at": now_iso,
            "tags": [],
            "file": _file_meta("../../x"),
        },
        {
            "entry_id": evil_entry_id,
            "folder_id": None,
            "display_name": "../../evil.sh",
            "lifecycle": "active",
            "created_at": now_iso,
            "updated_at": now_iso,
            "tags": [],
            "file": _file_meta(evil_file_id),
        },
    ]
    jsonl_names = (
        "folders.jsonl",
        "catalogs.jsonl",
        "views.jsonl",
        "tags.jsonl",
        "tag_aliases.jsonl",
        "entries.jsonl",
        "relations.jsonl",
    )
    manifest = {
        "format": "marginalia-knowledge-pack",
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "created_at": now_iso,
        "library_id": "hostile-library",
        "app_version": "0.0.0",
        "counts": {},
        "metadata_files": ["manifest.json", *jsonl_names],
    }
    latest = {
        "format": "marginalia-webdav-latest",
        "schema_version": 1,
        "library_id": "hostile-library",
        "snapshot_id": snapshot_id,
        "latest_snapshot": f"snapshots/{snapshot_id}/manifest.json",
        "updated_at": now_iso,
    }
    remote: dict[str, bytes] = {
        f"{_REMOTE_ROOT}/latest.json": (json.dumps(latest) + "\n").encode("utf-8"),
        f"{snap_root}/manifest.json": (json.dumps(manifest) + "\n").encode("utf-8"),
        f"{snap_root}/entries.jsonl": _jsonl_test_bytes(entries),
    }
    for name in jsonl_names:
        remote.setdefault(f"{snap_root}/{name}", b"")
    _MemoryWebDavClient.remote = remote

    pulled = await pull_latest_metadata()
    # The path-shaped file_id entry is skipped entirely.
    assert pulled["entries"] == 1

    factory = get_session_factory()
    async with factory() as session:
        assert await session.get(FileEntry, traversal_entry_id) is None
        assert await session.get(File, "../../x") is None

        evil = await session.get(FileEntry, evil_entry_id)
        assert evil is not None
        name = evil.display_name
        assert "/" not in name
        assert "\\" not in name
        assert name.strip(".") != ""
        assert "evil.sh" in name
        assert all(seg != ".." for seg in name.split("/"))

        storage_keys = (await session.execute(select(File.storage_key))).scalars().all()
        assert storage_keys  # the sanitized entry did import a file row
        assert all(".." not in key for key in storage_keys)


# ---------------------------------------------------------------------------
# 5. library_id publish guard + read-and-merge full publish (bug #35)
# ---------------------------------------------------------------------------


async def test_publish_refuses_foreign_library_then_merges_after_pull(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_a = _TEST_ROOT / "library_a"
    home_b = _TEST_ROOT / "library_b"
    _use_memory_client(monkeypatch)

    await _activate_home(home_a)
    entry_a = await _seed_entry(display_name="a-doc.txt", body=b"machine a doc\n")
    pub_a = await publish_snapshot()
    latest_path = f"{_REMOTE_ROOT}/latest.json"
    library_a = json.loads(
        _MemoryWebDavClient.remote[latest_path].decode("utf-8")
    )["library_id"]
    assert library_a

    await _activate_home(home_b)
    entry_b = await _seed_entry(display_name="b-doc.txt", body=b"machine b doc\n")
    with pytest.raises(WebDavConfigError):
        await publish_snapshot()
    # The refused publish must not have replaced the remote latest.
    latest_now = json.loads(_MemoryWebDavClient.remote[latest_path].decode("utf-8"))
    assert latest_now["snapshot_id"] == pub_a["snapshot_id"]
    assert latest_now["library_id"] == library_a

    # Now that machine B has its own library id, an implicit pull must also
    # refuse: it may not silently merge two unrelated libraries.
    with pytest.raises(WebDavConfigError):
        await pull_latest_metadata()

    # Deliberately join machine A's library, then the pull is allowed.
    _adopt_library_id(get_settings(), library_a)
    pulled = await pull_latest_metadata()
    assert pulled["entries"] == 1  # machine A's entry arrives on B

    # A remote-only entry B never pulled: full publish must carry it along.
    remote_only_entry = new_id()
    remote_only_file = new_id()
    remote_only_sha = "cd" * 32
    now_iso = datetime.now(timezone.utc).isoformat()
    entries_path = f"{_snapshot_root(str(pub_a['snapshot_id']))}/entries.jsonl"
    rows = _parse_jsonl(_MemoryWebDavClient.remote[entries_path])
    rows.append({
        "entry_id": remote_only_entry,
        "folder_id": None,
        "display_name": "remote-only.txt",
        "lifecycle": "active",
        "created_at": now_iso,
        "updated_at": now_iso,
        "tags": [],
        "file": {
            "file_id": remote_only_file,
            "sha256": remote_only_sha,
            "blob_path": f"blobs/sha256/cd/{remote_only_sha}",
            "size_bytes": 5,
        },
    })
    _MemoryWebDavClient.remote[entries_path] = _jsonl_test_bytes(rows)

    pub_b = await publish_snapshot()
    latest_after = json.loads(_MemoryWebDavClient.remote[latest_path].decode("utf-8"))
    assert latest_after["snapshot_id"] == pub_b["snapshot_id"]
    assert latest_after["library_id"] == library_a

    published = _parse_jsonl(
        _MemoryWebDavClient.remote[f"{_snapshot_root(str(pub_b['snapshot_id']))}/entries.jsonl"]
    )
    published_ids = {str(row["entry_id"]) for row in published}
    assert {
        entry_a["entry_id"],
        entry_b["entry_id"],
        remote_only_entry,
    } <= published_ids
