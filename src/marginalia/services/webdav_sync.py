"""WebDAV knowledge-pack publishing.

This is deliberately a publisher, not a live filesystem syncer. It writes
content-addressed blobs and immutable snapshot folders to WebDAV, then updates
`latest.json` last so consumers only see complete snapshots.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote
import uuid

import httpx
from sqlalchemy import delete, select

from marginalia.config import Settings, get_settings
from marginalia.db.models import (
    Catalog,
    Conversation,
    EntryRelation,
    EntryTag,
    File,
    FileEntry,
    Folder,
    Journal,
    Session,
    Tag,
    TagAlias,
    View,
)
from marginalia.db.session import session_scope
from marginalia.services.knowledge_pack import build_knowledge_pack, new_snapshot_id
from marginalia.storage import get_storage
from marginalia.storage.mirror import MirrorStorage
from marginalia.utils.ids import storage_prefix

_STATUS_REL = Path("sync") / "webdav_status.json"
_LIBRARY_ID_REL = Path("sync") / "library_id"


class WebDavConfigError(ValueError):
    pass


class WebDavClient:
    def __init__(self, settings: Settings) -> None:
        url = (settings.webdav_url or "").strip().rstrip("/")
        if not url:
            raise WebDavConfigError("WebDAV URL is not configured")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise WebDavConfigError("WebDAV URL must start with http:// or https://")
        auth = None
        if settings.webdav_username:
            auth = httpx.BasicAuth(
                settings.webdav_username,
                settings.webdav_password or "",
            )
        self._client = httpx.AsyncClient(
            base_url=url,
            auth=auth,
            timeout=httpx.Timeout(60.0, connect=20.0),
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def exists(self, path: str) -> bool:
        encoded = _encode_path(path)
        r = await self._client.request("HEAD", encoded)
        if r.status_code in {200, 204, 207}:
            return True
        if r.status_code == 404:
            return False
        if r.status_code in {405, 501}:
            r = await self._client.request(
                "PROPFIND",
                encoded,
                headers={"Depth": "0"},
                content=b"""<?xml version="1.0" encoding="utf-8"?><propfind xmlns="DAV:"><prop><resourcetype/></prop></propfind>""",
            )
            if r.status_code in {200, 207}:
                return True
            if r.status_code == 404:
                return False
        r.raise_for_status()
        return True

    async def mkcol(self, path: str) -> None:
        r = await self._client.request("MKCOL", _encode_path(path))
        if r.status_code in {200, 201, 204, 405}:
            return
        r.raise_for_status()

    async def ensure_dir(self, path: str) -> None:
        current = ""
        for part in _split_path(path):
            current = f"{current}/{part}"
            await self.mkcol(current)

    async def put_bytes(
        self,
        path: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> None:
        await self.ensure_dir(_parent_path(path))
        tmp = f"{path}.tmp-{uuid.uuid4().hex}"
        r = await self._client.put(
            _encode_path(tmp),
            content=body,
            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
        )
        r.raise_for_status()
        await self.move(tmp, path)

    async def put_stream(
        self,
        path: str,
        stream: AsyncIterator[bytes],
        *,
        content_type: str | None,
    ) -> None:
        await self.ensure_dir(_parent_path(path))
        tmp = f"{path}.tmp-{uuid.uuid4().hex}"
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        r = await self._client.put(
            _encode_path(tmp),
            content=stream,
            headers=headers,
        )
        r.raise_for_status()
        await self.move(tmp, path)

    async def move(self, src: str, dst: str) -> None:
        encoded_dst = str(self._client.base_url).rstrip("/") + _encode_path(dst)
        r = await self._client.request(
            "MOVE",
            _encode_path(src),
            headers={"Destination": encoded_dst, "Overwrite": "T"},
        )
        if r.status_code in {200, 201, 204}:
            return
        # A few simple WebDAV implementations do not support MOVE reliably.
        # For metadata writes, callers already uploaded to a unique temp path;
        # fail clearly rather than publishing latest.json before the move.
        r.raise_for_status()

    async def read_json(self, path: str) -> dict[str, Any] | None:
        r = await self._client.get(_encode_path(path))
        if r.status_code == 404:
            return None
        r.raise_for_status()
        try:
            payload = r.json()
        except ValueError as exc:
            raise WebDavConfigError(f"{path} is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise WebDavConfigError(f"{path} is not a JSON object")
        return payload

    async def read_bytes(self, path: str) -> bytes:
        r = await self._client.get(_encode_path(path))
        r.raise_for_status()
        return r.content

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
        storage = get_storage()
        hasher = hashlib.sha256() if expected_sha256 else None

        async def _stream_with_hash(source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
            async for chunk in source:
                if hasher is not None:
                    hasher.update(chunk)
                yield chunk

        async with self._client.stream("GET", _encode_path(path)) as r:
            r.raise_for_status()
            new_key = await storage.put(
                storage_key,
                _stream_with_hash(r.aiter_bytes()),
                content_type=content_type,
                display_name=display_name,
                folder_path=folder_path,
            )
        if hasher is not None:
            actual = hasher.hexdigest().lower()
            expected = expected_sha256.lower()
            if actual != expected:
                try:
                    await storage.delete(new_key)
                except Exception:
                    pass
                raise WebDavConfigError(
                    f"downloaded blob sha256 mismatch: expected {expected}, got {actual}"
                )
        return new_key


def configured(settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    return bool((s.webdav_url or "").strip() and (s.webdav_remote_path or "").strip())


async def test_connection(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    client = WebDavClient(s)
    try:
        root = _remote_root(s)
        await client.ensure_dir(root)
        latest = await client.read_json(_join_remote(root, "latest.json"))
        return {
            "ok": True,
            "remote_path": root,
            "latest": latest,
        }
    finally:
        await client.aclose()


async def publish_snapshot(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    if not configured(s):
        raise WebDavConfigError("WebDAV sync is not configured")

    library_id = _ensure_library_id(s)
    snapshot_id = new_snapshot_id()
    started_at = _now_iso()
    _write_status(s, {
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "snapshot_id": snapshot_id,
        "error": None,
    })

    uploaded_blobs = 0
    skipped_blobs = 0
    uploaded_metadata_files = 0
    root = _remote_root(s)
    snapshot_root = _join_remote(root, "snapshots", snapshot_id)
    storage = get_storage()
    client = WebDavClient(s)
    try:
        async with session_scope() as session:
            pack = await build_knowledge_pack(
                session,
                snapshot_id=snapshot_id,
                library_id=library_id,
            )

        await client.ensure_dir(root)
        await client.ensure_dir(_join_remote(root, "blobs"))
        await client.ensure_dir(snapshot_root)

        for blob in pack.blobs:
            remote_blob = _join_remote(root, blob.remote_path)
            if await client.exists(remote_blob):
                skipped_blobs += 1
                continue
            await client.put_stream(
                remote_blob,
                storage.get(blob.storage_key),
                content_type=blob.mime_type,
            )
            uploaded_blobs += 1

        for name, body in pack.metadata_files.items():
            content_type = (
                "application/json; charset=utf-8"
                if name.endswith(".json")
                else "application/x-ndjson; charset=utf-8"
                if name.endswith(".jsonl")
                else "text/markdown; charset=utf-8"
            )
            await client.put_bytes(
                _join_remote(snapshot_root, name),
                body,
                content_type=content_type,
            )
            uploaded_metadata_files += 1

        latest = {
            "format": "marginalia-webdav-latest",
            "schema_version": 1,
            "library_id": library_id,
            "snapshot_id": snapshot_id,
            "latest_snapshot": f"snapshots/{snapshot_id}/manifest.json",
            "updated_at": _now_iso(),
            "app_version": pack.manifest.get("app_version"),
        }
        await client.put_bytes(
            _join_remote(root, "latest.json"),
            (json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

        finished_at = _now_iso()
        result = {
            "ok": True,
            "status": "success",
            "started_at": started_at,
            "finished_at": finished_at,
            "snapshot_id": snapshot_id,
            "remote_path": root,
            "latest_snapshot": latest["latest_snapshot"],
            "uploaded_blobs": uploaded_blobs,
            "skipped_blobs": skipped_blobs,
            "uploaded_metadata_files": uploaded_metadata_files,
            "entry_count": pack.manifest["counts"]["entries"],
            "blob_count": pack.manifest["counts"]["blobs"],
            "blob_bytes": pack.manifest["counts"]["blob_bytes"],
            "error": None,
        }
        _write_status(s, result)
        return result
    except Exception as exc:
        failed = {
            "ok": False,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _now_iso(),
            "snapshot_id": snapshot_id,
            "remote_path": root,
            "uploaded_blobs": uploaded_blobs,
            "skipped_blobs": skipped_blobs,
            "uploaded_metadata_files": uploaded_metadata_files,
            "error": str(exc),
        }
        _write_status(s, failed)
        raise
    finally:
        await client.aclose()


async def pull_latest_metadata(settings: Settings | None = None) -> dict[str, Any]:
    """Import the remote latest snapshot metadata without downloading blobs."""
    s = settings or get_settings()
    if not configured(s):
        raise WebDavConfigError("WebDAV sync is not configured")
    root = _remote_root(s)
    client = WebDavClient(s)
    try:
        latest = await client.read_json(_join_remote(root, "latest.json"))
        if latest is None:
            raise WebDavConfigError("remote latest.json not found")
        latest_snapshot = str(latest.get("latest_snapshot") or "")
        if not latest_snapshot:
            raise WebDavConfigError("remote latest.json has no latest_snapshot")
        snapshot_root = _parent_path(_join_remote(root, latest_snapshot))
        manifest = await client.read_json(_join_remote(root, latest_snapshot))
        if manifest is None:
            raise WebDavConfigError("remote manifest not found")

        files: dict[str, list[dict[str, Any]]] = {}
        optional_files = {"sessions.jsonl", "conversations.jsonl", "journals.jsonl"}
        manifest_files = set(manifest.get("metadata_files") or [])
        for name in (
            "folders.jsonl",
            "catalogs.jsonl",
            "views.jsonl",
            "tags.jsonl",
            "tag_aliases.jsonl",
            "entries.jsonl",
            "relations.jsonl",
            "sessions.jsonl",
            "conversations.jsonl",
            "journals.jsonl",
        ):
            if name in optional_files and name not in manifest_files:
                files[name] = []
                continue
            body = await client.read_bytes(_join_remote(snapshot_root, name))
            files[name] = _parse_jsonl(body)

        async with session_scope() as session:
            result = await _import_metadata(
                session,
                root=root,
                latest=latest,
                manifest=manifest,
                rows=files,
            )
            await session.commit()

        status = read_status(s)
        last = dict(status.get("last") or {})
        last.update({
            "last_pull_at": _now_iso(),
            "last_pulled_snapshot_id": manifest.get("snapshot_id"),
            "last_pull": result,
        })
        _write_status(s, last)
        return {
            "ok": True,
            "remote_path": root,
            "snapshot_id": manifest.get("snapshot_id"),
            **result,
        }
    finally:
        await client.aclose()


async def hydrate_entry(entry_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """Download one remote-backed entry's blob into the local storage backend."""
    s = settings or get_settings()
    async with session_scope() as session:
        row = (
            await session.execute(
                select(FileEntry, File)
                .join(File, File.id == FileEntry.file_id)
                .where(
                    FileEntry.id == entry_id,
                    FileEntry.deleted_at.is_(None),
                    File.deleted_at.is_(None),
                )
            )
        ).first()
        if row is None:
            raise WebDavConfigError("entry not found")
        entry, file_row = row
        marker = _remote_marker(file_row.description)
        if not marker:
            return {
                "ok": True,
                "entry_id": entry.id,
                "file_id": file_row.id,
                "hydrated": True,
                "already_local": True,
            }
        if marker.get("hydrated"):
            return {
                "ok": True,
                "entry_id": entry.id,
                "file_id": file_row.id,
                "hydrated": True,
                "already_local": True,
            }
        remote_root = str(marker.get("remote_root") or _remote_root(s))
        blob_path = str(marker.get("blob_path") or "")
        if not blob_path:
            raise WebDavConfigError("remote entry has no blob_path")
        remote_blob = _join_remote(remote_root, blob_path)
        folder_path = await _folder_path(session, entry.folder_id)

    client = WebDavClient(s)
    try:
        new_storage_key = await client.stream_to_storage(
            remote_blob,
            storage_key=file_row.storage_key,
            display_name=entry.display_name,
            folder_path=folder_path,
            content_type=file_row.mime_type,
            expected_sha256=str(marker.get("sha256") or file_row.sha256 or "") or None,
        )
    finally:
        await client.aclose()

    async with session_scope() as session:
        pair = (
            await session.execute(
                select(FileEntry, File)
                .join(File, File.id == FileEntry.file_id)
                .where(FileEntry.id == entry_id)
            )
        ).first()
        if pair is None:
            raise WebDavConfigError("entry disappeared during hydrate")
        entry, file_row = pair
        description = _description_with_remote(
            file_row.description,
            {
                **(_remote_marker(file_row.description) or {}),
                "hydrated": True,
                "hydrated_at": _now_iso(),
            },
        )
        file_row.storage_key = new_storage_key
        file_row.description = description
        file_row.updated_at = datetime.now(timezone.utc)
        await session.commit()
        return {
            "ok": True,
            "entry_id": entry.id,
            "file_id": file_row.id,
            "hydrated": True,
            "storage_key": new_storage_key,
        }


def read_status(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    status_path = _status_path(s)
    status: dict[str, Any] = {}
    if status_path.exists():
        try:
            raw = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                status = raw
        except (OSError, json.JSONDecodeError):
            status = {}
    return {
        "configured": configured(s),
        "url": s.webdav_url,
        "username": s.webdav_username,
        "password_set": bool(s.webdav_password),
        "remote_path": s.webdav_remote_path,
        "auto_sync_enabled": s.webdav_auto_sync_enabled,
        "auto_sync_interval_minutes": s.webdav_auto_sync_interval_minutes,
        "last": status or None,
    }


async def _import_metadata(
    session,
    *,
    root: str,
    latest: dict[str, Any],
    manifest: dict[str, Any],
    rows: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    imported = {
        "folders": 0,
        "catalogs": 0,
        "views": 0,
        "tags": 0,
        "tag_aliases": 0,
        "entries": 0,
        "entry_tags": 0,
        "relations": 0,
        "sessions": 0,
        "conversations": 0,
        "journals": 0,
        "remote_files": 0,
    }
    storage = get_storage()
    for item in rows.get("folders.jsonl", []):
        folder_id = str(item.get("folder_id") or "")
        if not folder_id:
            continue
        row = await session.get(Folder, folder_id)
        if row is None:
            row = Folder(id=folder_id, created_at=_parse_dt(item.get("created_at")) or now)
            session.add(row)
        row.parent_id = item.get("parent_id")
        row.name = str(item.get("name") or "Untitled")
        row.updated_at = _parse_dt(item.get("updated_at")) or now
        row.deleted_at = None
        imported["folders"] += 1
        await session.flush()

    for item in rows.get("catalogs.jsonl", []):
        catalog_id = str(item.get("catalog_id") or "")
        if not catalog_id:
            continue
        row = await session.get(Catalog, catalog_id)
        if row is None:
            row = Catalog(id=catalog_id, created_at=_parse_dt(item.get("created_at")) or now)
            session.add(row)
        row.parent_id = item.get("parent_id")
        row.name = str(item.get("name") or "Untitled")
        row.summary = item.get("summary")
        row.description = item.get("description")
        row.extra = item.get("extra")
        row.tags = item.get("tags")
        row.is_system = bool(item.get("is_system"))
        row.updated_at = _parse_dt(item.get("updated_at")) or now
        row.deleted_at = None
        imported["catalogs"] += 1
        await session.flush()

    for item in rows.get("views.jsonl", []):
        view_id = str(item.get("view_id") or "")
        if not view_id:
            continue
        row = await session.get(View, view_id)
        if row is None:
            row = View(id=view_id, created_at=_parse_dt(item.get("created_at")) or now)
            session.add(row)
        row.name = str(item.get("name") or "Untitled")
        row.summary = item.get("summary")
        row.description = item.get("description")
        row.extra = item.get("extra")
        row.tags = item.get("tags")
        row.filter_spec = item.get("filter_spec") or {}
        row.updated_at = _parse_dt(item.get("updated_at")) or now
        row.deleted_at = None
        imported["views"] += 1

    tag_alias_of_updates: list[tuple[str, str | None]] = []
    for item in rows.get("tags.jsonl", []):
        tag_id = str(item.get("tag_id") or "")
        if not tag_id:
            continue
        row = await session.get(Tag, tag_id)
        if row is None:
            row = Tag(id=tag_id, created_at=_parse_dt(item.get("created_at")) or now)
            session.add(row)
        row.name = str(item.get("name") or "untitled")
        row.facet = str(item.get("facet") or "extra")
        row.alias_of = None
        row.doc_count = int(item.get("doc_count") or 0)
        row.last_used_at = _parse_dt(item.get("last_used_at"))
        row.last_reaffirmed_at = _parse_dt(item.get("last_reaffirmed_at"))
        row.reaffirm_count = int(item.get("reaffirm_count") or 0)
        row.updated_at = _parse_dt(item.get("updated_at")) or now
        tag_alias_of_updates.append((tag_id, item.get("alias_of")))
        imported["tags"] += 1

    if rows.get("tags.jsonl"):
        await session.flush()
        for tag_id, alias_of in tag_alias_of_updates:
            row = await session.get(Tag, tag_id)
            if row is None:
                continue
            if alias_of and await session.get(Tag, str(alias_of)):
                row.alias_of = str(alias_of)
        await session.flush()

    for item in rows.get("tag_aliases.jsonl", []):
        alias_id = str(item.get("tag_alias_id") or "")
        to_tag_id = str(item.get("to_tag_id") or "")
        if not alias_id or not to_tag_id:
            continue
        row = await session.get(TagAlias, alias_id)
        if row is None:
            row = TagAlias(id=alias_id)
            session.add(row)
        row.from_name = str(item.get("from_name") or "")
        row.to_tag_id = to_tag_id
        row.note = item.get("note")
        row.created_at = _parse_dt(item.get("created_at")) or now
        imported["tag_aliases"] += 1

    if rows.get("tags.jsonl") or rows.get("folders.jsonl") or rows.get("catalogs.jsonl"):
        await session.flush()

    for item in rows.get("entries.jsonl", []):
        file_meta = item.get("file") if isinstance(item.get("file"), dict) else {}
        file_id = str(file_meta.get("file_id") or item.get("file_id") or "")
        entry_id = str(item.get("entry_id") or "")
        if not file_id or not entry_id:
            continue
        file_row = await session.get(File, file_id)
        created_at = _parse_dt(file_meta.get("created_at")) or now
        existing_marker = _remote_marker(file_row.description) if file_row is not None else None
        hydrated = False
        if file_row is not None:
            hydrated = bool(existing_marker and existing_marker.get("hydrated"))
            if not hydrated:
                try:
                    hydrated = await storage.exists(file_row.storage_key)
                except Exception:
                    hydrated = False
        marker = {
            "remote_root": root,
            "library_id": manifest.get("library_id") or latest.get("library_id"),
            "snapshot_id": manifest.get("snapshot_id") or latest.get("snapshot_id"),
            "blob_path": file_meta.get("blob_path"),
            "sha256": file_meta.get("sha256"),
            "hydrated": hydrated,
            "imported_at": _now_iso(),
        }
        if hydrated:
            marker["hydrated_at"] = (
                existing_marker.get("hydrated_at")
                if existing_marker
                else _now_iso()
            )
        if file_row is None:
            storage_key = _placeholder_storage_key(
                file_id=file_id,
                display_name=str(item.get("display_name") or file_id),
                folder_path=None,
            )
            file_row = File(
                id=file_id,
                storage_key=storage_key,
                sha256=str(file_meta.get("sha256") or ""),
                size_bytes=int(file_meta.get("size_bytes") or 0),
                created_at=created_at,
                updated_at=_parse_dt(file_meta.get("updated_at")) or now,
            )
            session.add(file_row)
            imported["remote_files"] += 1
        file_row.sha256 = str(file_meta.get("sha256") or file_row.sha256)
        file_row.size_bytes = int(file_meta.get("size_bytes") or file_row.size_bytes or 0)
        file_row.mime_type = file_meta.get("mime_type")
        file_row.original_ext = file_meta.get("original_ext")
        file_row.kind = file_meta.get("kind")
        file_row.summary = file_meta.get("summary")
        file_row.description = _description_with_remote(file_meta.get("description"), marker)
        file_row.extra = file_meta.get("extra")
        file_row.ingest_status = str(file_meta.get("ingest_status") or "done")
        file_row.ingested_at = _parse_dt(file_meta.get("ingested_at"))
        file_row.deleted_at = None
        await session.flush()

        entry_row = await session.get(FileEntry, entry_id)
        if entry_row is None:
            entry_row = FileEntry(id=entry_id, created_at=_parse_dt(item.get("created_at")) or now)
            session.add(entry_row)
        entry_row.folder_id = item.get("folder_id")
        entry_row.file_id = file_id
        entry_row.display_name = str(item.get("display_name") or "Untitled")
        entry_row.lifecycle = str(item.get("lifecycle") or "active")
        entry_row.catalog_id = item.get("catalog_id")
        entry_row.extra = item.get("extra")
        entry_row.deleted_at = None
        entry_row.purge_after = None
        entry_row.updated_at = _parse_dt(item.get("updated_at")) or now
        imported["entries"] += 1
        await session.flush()

        await session.execute(delete(EntryTag).where(EntryTag.entry_id == entry_id))
        for tag in item.get("tags") or []:
            if not isinstance(tag, dict) or not tag.get("tag_id"):
                continue
            session.add(EntryTag(
                entry_id=entry_id,
                tag_id=str(tag["tag_id"]),
                source=str(tag.get("source") or "ingest"),
                created_at=_parse_dt(tag.get("created_at")) or now,
                last_reaffirmed_at=_parse_dt(tag.get("last_reaffirmed_at")),
                reaffirm_count=int(tag.get("reaffirm_count") or 0),
            ))
            imported["entry_tags"] += 1

    if rows.get("entries.jsonl"):
        await session.flush()

    for item in rows.get("relations.jsonl", []):
        relation_id = str(item.get("relation_id") or "")
        entry_a_id = str(item.get("entry_a_id") or "")
        entry_b_id = str(item.get("entry_b_id") or "")
        if not relation_id or not entry_a_id or not entry_b_id:
            continue
        row = await session.get(EntryRelation, relation_id)
        if row is None:
            row = (
                await session.execute(
                    select(EntryRelation).where(
                        EntryRelation.entry_a_id == entry_a_id,
                        EntryRelation.entry_b_id == entry_b_id,
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            row = EntryRelation(id=relation_id)
            session.add(row)
        row.entry_a_id = entry_a_id
        row.entry_b_id = entry_b_id
        row.note = str(item.get("note") or "")
        row.source_kind = str(item.get("source_kind") or "mine_relations")
        row.last_observed_at = _parse_dt(item.get("last_observed_at")) or now
        row.observation_count = int(item.get("observation_count") or 1)
        row.vetted = item.get("vetted")
        row.vetted_reason = item.get("vetted_reason")
        row.vetted_at = _parse_dt(item.get("vetted_at"))
        row.vetted_observation_count = item.get("vetted_observation_count")
        row.created_at = _parse_dt(item.get("created_at")) or now
        imported["relations"] += 1

    for item in rows.get("sessions.jsonl", []):
        session_id = str(item.get("session_id") or "")
        if not session_id:
            continue
        row = await session.get(Session, session_id)
        if row is None:
            row = Session(
                id=session_id,
                started_at=_parse_dt(item.get("started_at")) or now,
                initiating_user_message=str(item.get("initiating_user_message") or ""),
            )
            session.add(row)
        row.started_at = _parse_dt(item.get("started_at")) or row.started_at or now
        row.ended_at = _parse_dt(item.get("ended_at"))
        row.end_reason = item.get("end_reason")
        row.deleted_at = _parse_dt(item.get("deleted_at"))
        row.initiating_user_message = str(item.get("initiating_user_message") or "")
        row.turn_count = _as_int(item.get("turn_count"), 0)
        row.total_input_tokens = _as_int(item.get("total_input_tokens"), 0)
        row.total_output_tokens = _as_int(item.get("total_output_tokens"), 0)
        row.total_cache_read = _as_int(item.get("total_cache_read"), 0)
        row.total_tool_calls = _as_int(item.get("total_tool_calls"), 0)
        row.total_llm_calls = _as_int(item.get("total_llm_calls"), 0)
        row.total_cost_estimate = _as_decimal(item.get("total_cost_estimate"))
        row.total_duration_ms = _as_int(item.get("total_duration_ms"), 0)
        imported["sessions"] += 1

    if rows.get("conversations.jsonl"):
        await session.flush()

    for item in rows.get("conversations.jsonl", []):
        conversation_id = str(item.get("conversation_id") or "")
        session_id = str(item.get("session_id") or "")
        if not conversation_id or not session_id:
            continue
        if await session.get(Session, session_id) is None:
            continue
        row = await session.get(Conversation, conversation_id)
        if row is None:
            row = Conversation(
                id=conversation_id,
                session_id=session_id,
                turn_index=_as_int(item.get("turn_index"), 0),
                started_at=_parse_dt(item.get("started_at")) or now,
                user_message=str(item.get("user_message") or ""),
            )
            session.add(row)
        row.session_id = session_id
        row.turn_index = _as_int(item.get("turn_index"), 0)
        row.started_at = _parse_dt(item.get("started_at")) or row.started_at or now
        row.ended_at = _parse_dt(item.get("ended_at"))
        row.user_message = str(item.get("user_message") or "")
        row.agent_response = item.get("agent_response")
        row.tool_calls = item.get("tool_calls") if isinstance(item.get("tool_calls"), list) else []
        row.llm_calls = item.get("llm_calls") if isinstance(item.get("llm_calls"), list) else []
        row.total_input_tokens = _as_int(item.get("total_input_tokens"), 0)
        row.total_output_tokens = _as_int(item.get("total_output_tokens"), 0)
        row.total_cache_read = _as_int(item.get("total_cache_read"), 0)
        row.total_tool_calls = _as_int(item.get("total_tool_calls"), 0)
        row.total_llm_calls = _as_int(item.get("total_llm_calls"), 0)
        row.total_duration_ms = _as_int(item.get("total_duration_ms"), 0)
        row.total_cost_estimate = _as_decimal(item.get("total_cost_estimate"))
        imported["conversations"] += 1

    if rows.get("journals.jsonl"):
        await session.flush()

    journal_link_updates: list[dict[str, Any]] = []
    for item in rows.get("journals.jsonl", []):
        journal_id = str(item.get("journal_id") or "")
        conversation_id = str(item.get("conversation_id") or "")
        if not journal_id or not conversation_id:
            continue
        if await session.get(Conversation, conversation_id) is None:
            continue
        row = await session.get(Journal, journal_id)
        if row is None:
            row = Journal(
                id=journal_id,
                conversation_id=conversation_id,
                note=str(item.get("note") or ""),
                created_at=_parse_dt(item.get("created_at")) or now,
            )
            session.add(row)
        row.conversation_id = conversation_id
        row.note = str(item.get("note") or "")
        row.entry_ids = item.get("entry_ids") if isinstance(item.get("entry_ids"), list) else []
        row.tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        row.source_kind = str(item.get("source_kind") or "reflect_turn")
        row.superseded_by_id = None
        row.summarized_journal_ids = item.get("summarized_journal_ids")
        row.invalidated_at = _parse_dt(item.get("invalidated_at"))
        row.invalidated_by_id = None
        row.invalidated_reason = item.get("invalidated_reason")
        row.created_at = _parse_dt(item.get("created_at")) or row.created_at or now
        journal_link_updates.append(item)
        imported["journals"] += 1

    if journal_link_updates:
        await session.flush()
        for item in journal_link_updates:
            journal_id = str(item.get("journal_id") or "")
            row = await session.get(Journal, journal_id)
            if row is None:
                continue
            superseded_by_id = item.get("superseded_by_id")
            if superseded_by_id and await session.get(Journal, str(superseded_by_id)):
                row.superseded_by_id = str(superseded_by_id)
            invalidated_by_id = item.get("invalidated_by_id")
            if invalidated_by_id and await session.get(Journal, str(invalidated_by_id)):
                row.invalidated_by_id = str(invalidated_by_id)

    return imported


def _ensure_library_id(settings: Settings) -> str:
    path = _library_id_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(uuid.uuid4())
    path.write_text(value + "\n", encoding="utf-8")
    return value


def _write_status(settings: Settings, value: dict[str, Any]) -> None:
    path = _status_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=".webdav_status.",
        suffix=".json",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _status_path(settings: Settings) -> Path:
    return Path(settings.marginalia_home).expanduser() / _STATUS_REL


def _library_id_path(settings: Settings) -> Path:
    return Path(settings.marginalia_home).expanduser() / _LIBRARY_ID_REL


def _remote_root(settings: Settings) -> str:
    return "/" + "/".join(_split_path(settings.webdav_remote_path or "/marginalia"))


def _parse_jsonl(body: bytes) -> list[dict[str, Any]]:
    text = body.decode("utf-8")
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            out.append(value)
    return out


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except Exception:
        return Decimal("0")


def _description_with_remote(description: Any, marker: dict[str, Any]) -> dict[str, Any]:
    if isinstance(description, dict):
        out = dict(description)
    else:
        out = {}
        if description is not None:
            out["imported_description"] = description
    out["_webdav_remote"] = marker
    return out


def _remote_marker(description: Any) -> dict[str, Any] | None:
    if not isinstance(description, dict):
        return None
    marker = description.get("_webdav_remote")
    return dict(marker) if isinstance(marker, dict) else None


def webdav_remote_marker(description: Any) -> dict[str, Any] | None:
    """Public helper for routes/user metadata to expose remote state."""
    return _remote_marker(description)


def _placeholder_storage_key(
    *,
    file_id: str,
    display_name: str,
    folder_path: str | None,
) -> str:
    storage = get_storage()
    if isinstance(storage, MirrorStorage):
        parts = ["_webdav", file_id]
        return "/".join(parts)
    top, sub = storage_prefix(file_id)
    return f"{top}/{sub}/{file_id}"


async def _folder_path(session, folder_id: str | None) -> str | None:
    if folder_id is None:
        return None
    parts: list[str] = []
    cur = folder_id
    while cur:
        folder = await session.get(Folder, cur)
        if folder is None or folder.deleted_at is not None:
            break
        parts.append(folder.name)
        cur = folder.parent_id
    return "/" + "/".join(reversed(parts)) if parts else None


def _join_remote(*parts: str) -> str:
    joined: list[str] = []
    for part in parts:
        joined.extend(_split_path(part))
    return "/" + "/".join(joined)


def _parent_path(path: str) -> str:
    parts = _split_path(path)
    if len(parts) <= 1:
        return "/"
    return "/" + "/".join(parts[:-1])


def _split_path(path: str) -> list[str]:
    return [part for part in str(path).replace("\\", "/").split("/") if part]


def _encode_path(path: str) -> str:
    return "/" + "/".join(quote(part, safe="") for part in _split_path(path))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
