"""WebDAV knowledge-pack sync smoke tests.

Run:
    .venv/Scripts/python -m pytest tests/test_webdav_sync_e2e.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

_TEST_PARENT = Path(os.environ.get(
    "MARGINALIA_TEST_TMP",
    str(Path(__file__).resolve().parent),
))
_TEST_ROOT = _TEST_PARENT / f"_webdav_sync_e2e_{os.getpid()}_{uuid4().hex[:8]}"
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
    Conversation,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Journal,
    Session,
    Tag,
)
from marginalia.services.knowledge_pack import build_knowledge_pack  # noqa: E402
from marginalia.services.user_files import (  # noqa: E402
    EntryNotFoundError,
    collect_folder_entries,
    get_user_metadata,
    open_for_download,
)
from marginalia.services.webdav_sync import (  # noqa: E402
    WebDavClient,
    WebDavConfigError,
    hydrate_entry,
    list_remote_entries,
    pull_latest_metadata,
)
from marginalia.storage import get_storage, reset_storage_cache  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402


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


async def _seed_source() -> dict[str, str | bytes]:
    now = datetime.now(timezone.utc)
    body = b"WebDAV sync source document.\n" * 4
    sha = hashlib.sha256(body).hexdigest()
    await get_storage().put(
        "aa/bb/source",
        _one_chunk(body),
        content_type="text/plain",
    )
    factory = get_session_factory()
    async with factory() as session:
        folder = Folder(
            id=new_id(),
            parent_id=None,
            name="sync",
            created_at=now,
            updated_at=now,
        )
        file_row = File(
            id=new_id(),
            storage_key="aa/bb/source",
            sha256=sha,
            size_bytes=len(body),
            mime_type="text/plain",
            original_ext=".txt",
            kind="text",
            summary="Source summary",
            description={"sections": [{"title": "Overview", "summary": "sync"}]},
            extra="file extra",
            ingest_status="done",
            ingested_at=now,
            created_at=now,
            updated_at=now,
        )
        entry = FileEntry(
            id=new_id(),
            folder_id=folder.id,
            file_id=file_row.id,
            display_name="source.txt",
            lifecycle="active",
            catalog_id=None,
            extra="entry extra",
            created_at=now,
            updated_at=now,
        )
        tag = Tag(
            id=new_id(),
            name="webdav",
            facet="topic",
            alias_of=None,
            doc_count=1,
            last_used_at=now,
            created_at=now,
            updated_at=now,
        )
        sess = Session(
            id=new_id(),
            started_at=now,
            ended_at=now,
            end_reason="normal",
            initiating_user_message="sync test",
            turn_count=1,
            total_input_tokens=1,
            total_output_tokens=2,
            total_cache_read=0,
            total_tool_calls=0,
            total_llm_calls=0,
            total_cost_estimate=Decimal("0"),
            total_duration_ms=0,
        )
        conv = Conversation(
            id=new_id(),
            session_id=sess.id,
            turn_index=0,
            started_at=now,
            ended_at=now,
            user_message="sync test",
            agent_response="ok",
            tool_calls=[],
            llm_calls=[],
            total_input_tokens=1,
            total_output_tokens=2,
            total_cache_read=0,
            total_tool_calls=0,
            total_llm_calls=0,
            total_cost_estimate=Decimal("0"),
            total_duration_ms=0,
        )
        journal = Journal(
            id=new_id(),
            conversation_id=conv.id,
            note="Remember that this file came from the WebDAV test.",
            entry_ids=[entry.id],
            tags=["webdav"],
            source_kind="insight",
            summarized_journal_ids=[],
            created_at=now,
        )
        session.add_all([folder, file_row, tag, sess])
        await session.flush()
        session.add(entry)
        await session.flush()
        session.add(conv)
        await session.flush()
        session.add(journal)
        await session.flush()
        session.add(EntryTag(
            entry_id=entry.id,
            tag_id=tag.id,
            source="ingest",
            created_at=now,
        ))
        await session.commit()
        return {
            "body": body,
            "entry_id": entry.id,
            "folder_id": folder.id,
            "journal_id": journal.id,
        }


async def _build_remote_pack() -> tuple[dict[str, bytes], dict[str, object]]:
    root = "/marginalia-test"
    snapshot_id = "2026-07-01T00-00-00Z"
    factory = get_session_factory()
    async with factory() as session:
        pack = await build_knowledge_pack(
            session,
            snapshot_id=snapshot_id,
            library_id="library-test",
        )
    remote: dict[str, bytes] = {}
    snapshot_root = f"{root}/snapshots/{snapshot_id}"
    for name, body in pack.metadata_files.items():
        remote[f"{snapshot_root}/{name}"] = body
    for blob in pack.blobs:
        remote[f"{root}/{blob.remote_path}"] = await _read_storage(blob.storage_key)
    latest = {
        "format": "marginalia-webdav-latest",
        "schema_version": 1,
        "library_id": "library-test",
        "snapshot_id": snapshot_id,
        "latest_snapshot": f"snapshots/{snapshot_id}/manifest.json",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "app_version": pack.manifest.get("app_version"),
    }
    remote[f"{root}/latest.json"] = (
        json.dumps(latest, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return remote, pack.manifest


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


async def test_pull_metadata_then_hydrate_on_demand(monkeypatch: pytest.MonkeyPatch) -> None:
    source_home = _TEST_ROOT / "source"
    dest_home = _TEST_ROOT / "dest"
    await _activate_home(source_home)
    seeded = await _seed_source()
    remote, manifest = await _build_remote_pack()
    assert manifest["counts"]["sessions"] == 1
    assert manifest["counts"]["conversations"] == 1
    assert manifest["counts"]["journals"] == 1
    assert "sessions.jsonl" in manifest["metadata_files"]
    assert "conversations.jsonl" in manifest["metadata_files"]
    assert "journals.jsonl" in manifest["metadata_files"]

    await _activate_home(dest_home)
    _MemoryWebDavClient.remote = remote
    monkeypatch.setattr(
        "marginalia.services.webdav_sync.WebDavClient",
        _MemoryWebDavClient,
    )

    pulled = await pull_latest_metadata()
    assert pulled["entries"] == 1
    assert pulled["remote_files"] == 1
    assert pulled["sessions"] == 1
    assert pulled["conversations"] == 1
    assert pulled["journals"] == 1

    factory = get_session_factory()
    async with factory() as session:
        with pytest.raises(EntryNotFoundError):
            await get_user_metadata(session, entry_id=str(seeded["entry_id"]))
        with pytest.raises(EntryNotFoundError):
            await open_for_download(session, entry_id=str(seeded["entry_id"]))
        assert await session.get(Journal, str(seeded["journal_id"])) is not None

    remote_entries = await list_remote_entries()
    assert remote_entries["total"] == 1
    assert remote_entries["entries"][0]["entry_id"] == seeded["entry_id"]
    assert remote_entries["entries"][0]["folder_path"] == "/sync"

    hydrated = await hydrate_entry(str(seeded["entry_id"]))
    assert hydrated["hydrated"] is True

    async with factory() as session:
        meta = await get_user_metadata(session, entry_id=str(seeded["entry_id"]))
        assert meta["webdav_remote"]["hydrated"] is True
        handle = await open_for_download(session, entry_id=str(seeded["entry_id"]))
        downloaded = bytearray()
        async for chunk in handle.stream:
            downloaded.extend(chunk)
        assert bytes(downloaded) == seeded["body"]
        members = await collect_folder_entries(session, folder_id=str(seeded["folder_id"]))
        assert [member[0] for member in members] == ["source.txt"]
        assert await session.scalar(select(func.count(Session.id))) == 1
        assert await session.scalar(select(func.count(Conversation.id))) == 1
        assert await session.scalar(select(func.count(Journal.id))) == 1


async def test_webdav_stream_to_storage_checks_sha256() -> None:
    await _activate_home(_TEST_ROOT / "hash")
    body = b"verified webdav blob"
    expected = hashlib.sha256(body).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, content=body)

    client = WebDavClient(get_settings())
    await client._client.aclose()  # type: ignore[attr-defined]
    client._client = httpx.AsyncClient(  # type: ignore[attr-defined]
        base_url="https://dav.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        key = await client.stream_to_storage(
            "/blob",
            storage_key="ok/blob",
            display_name="blob.txt",
            folder_path=None,
            content_type="text/plain",
            expected_sha256=expected,
        )
        assert await _read_storage(key) == body
        with pytest.raises(WebDavConfigError):
            await client.stream_to_storage(
                "/blob",
                storage_key="bad/blob",
                display_name="blob.txt",
                folder_path=None,
                content_type="text/plain",
                expected_sha256="0" * 64,
            )
        assert not await get_storage().exists("bad/blob")
    finally:
        await client.aclose()
