"""Knowledge-pack snapshot builder.

A knowledge pack is a portable, WebDAV-friendly snapshot of the local
library. It is not a live database replica. The pack records user-visible
facts and AI-derived metadata as JSON/JSONL, while original bytes are
addressed by sha256 so WebDAV sync can skip blobs already present remotely.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import platform
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia import __version__
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

PACK_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BlobSource:
    sha256: str
    size_bytes: int
    storage_key: str
    mime_type: str | None

    @property
    def remote_path(self) -> str:
        return blob_path(self.sha256)


@dataclass(frozen=True)
class KnowledgePack:
    snapshot_id: str
    created_at: str
    manifest: dict[str, Any]
    metadata_files: dict[str, bytes]
    blobs: list[BlobSource]


def new_snapshot_id(_now: datetime | None = None) -> str:
    """Return a short, path-safe snapshot identifier.

    Timestamps live in manifest.created_at/latest.updated_at. The snapshot id is
    intentionally Git-like so it works as a stable directory/key identifier
    without looking like the user-facing time.
    """
    return secrets.token_hex(8)


def blob_path(sha256: str) -> str:
    return f"blobs/sha256/{sha256[:2]}/{sha256}"


async def build_knowledge_pack(
    session: AsyncSession,
    *,
    snapshot_id: str,
    library_id: str,
) -> KnowledgePack:
    created = datetime.now(timezone.utc)

    folder_rows = (
        await session.execute(
            select(Folder)
            .where(Folder.deleted_at.is_(None))
            .order_by(Folder.created_at.asc())
        )
    ).scalars().all()

    entry_file_rows = (
        await session.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(FileEntry.deleted_at.is_(None), File.deleted_at.is_(None))
            .order_by(FileEntry.created_at.asc())
        )
    ).all()

    live_entry_ids = [entry.id for entry, _file in entry_file_rows]
    live_entry_id_set = set(live_entry_ids)

    tags_by_entry: dict[str, list[dict[str, Any]]] = {entry_id: [] for entry_id in live_entry_ids}
    if live_entry_ids:
        tag_rows = (
            await session.execute(
                select(EntryTag, Tag)
                .join(Tag, Tag.id == EntryTag.tag_id)
                .where(EntryTag.entry_id.in_(live_entry_ids))
                .order_by(EntryTag.entry_id.asc(), Tag.facet.asc(), Tag.name.asc())
            )
        ).all()
        for entry_tag, tag in tag_rows:
            tags_by_entry.setdefault(entry_tag.entry_id, []).append({
                "tag_id": tag.id,
                "name": tag.name,
                "facet": tag.facet,
                "source": entry_tag.source,
                "created_at": _dt(entry_tag.created_at),
                "last_reaffirmed_at": _dt(entry_tag.last_reaffirmed_at),
                "reaffirm_count": entry_tag.reaffirm_count,
            })

    blob_by_sha: dict[str, BlobSource] = {}
    entries: list[dict[str, Any]] = []
    for entry, file_row in entry_file_rows:
        blob_by_sha.setdefault(
            file_row.sha256,
            BlobSource(
                sha256=file_row.sha256,
                size_bytes=file_row.size_bytes,
                storage_key=file_row.storage_key,
                mime_type=file_row.mime_type,
            ),
        )
        entries.append({
            "entry_id": entry.id,
            "folder_id": entry.folder_id,
            "file_id": entry.file_id,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "catalog_id": entry.catalog_id,
            "extra": entry.extra,
            "created_at": _dt(entry.created_at),
            "updated_at": _dt(entry.updated_at),
            "tags": tags_by_entry.get(entry.id, []),
            "file": {
                "file_id": file_row.id,
                "sha256": file_row.sha256,
                "blob_path": blob_path(file_row.sha256),
                "size_bytes": file_row.size_bytes,
                "mime_type": file_row.mime_type,
                "original_ext": file_row.original_ext,
                "kind": file_row.kind,
                "summary": file_row.summary,
                "description": file_row.description,
                "extra": file_row.extra,
                "ingest_status": file_row.ingest_status,
                "ingested_at": _dt(file_row.ingested_at),
                "created_at": _dt(file_row.created_at),
                "updated_at": _dt(file_row.updated_at),
            },
        })

    catalogs = [
        _catalog_record(row)
        for row in (
            await session.execute(
                select(Catalog)
                .where(Catalog.deleted_at.is_(None))
                .order_by(Catalog.created_at.asc())
            )
        ).scalars().all()
    ]
    views = [
        _view_record(row)
        for row in (
            await session.execute(
                select(View)
                .where(View.deleted_at.is_(None))
                .order_by(View.created_at.asc())
            )
        ).scalars().all()
    ]
    tags = [
        _tag_record(row)
        for row in (
            await session.execute(select(Tag).order_by(Tag.facet.asc(), Tag.name.asc()))
        ).scalars().all()
    ]
    tag_aliases = [
        _tag_alias_record(row)
        for row in (
            await session.execute(select(TagAlias).order_by(TagAlias.created_at.asc()))
        ).scalars().all()
    ]

    relations: list[dict[str, Any]] = []
    if live_entry_ids:
        relation_rows = (
            await session.execute(
                select(EntryRelation)
                .where(
                    EntryRelation.entry_a_id.in_(live_entry_ids),
                    EntryRelation.entry_b_id.in_(live_entry_ids),
                )
                .order_by(EntryRelation.created_at.asc())
            )
        ).scalars().all()
        relations = [
            _relation_record(row)
            for row in relation_rows
            if row.entry_a_id in live_entry_id_set and row.entry_b_id in live_entry_id_set
        ]

    journal_rows = (
        await session.execute(select(Journal).order_by(Journal.created_at.asc()))
    ).scalars().all()
    journal_conversation_ids = {
        row.conversation_id for row in journal_rows if row.conversation_id
    }
    conversation_rows = []
    if journal_conversation_ids:
        conversation_rows = (
            await session.execute(
                select(Conversation)
                .where(Conversation.id.in_(journal_conversation_ids))
                .order_by(Conversation.started_at.asc())
            )
        ).scalars().all()
    conversation_session_ids = {
        row.session_id for row in conversation_rows if row.session_id
    }
    session_rows = []
    if conversation_session_ids:
        session_rows = (
            await session.execute(
                select(Session)
                .where(Session.id.in_(conversation_session_ids))
                .order_by(Session.started_at.asc())
            )
        ).scalars().all()

    sessions = [_session_record(row) for row in session_rows]
    conversations = [_conversation_record(row) for row in conversation_rows]
    journals = [_journal_record(row) for row in journal_rows]

    folders = [_folder_record(row) for row in folder_rows]
    blobs = sorted(blob_by_sha.values(), key=lambda b: b.sha256)
    created_at = created.isoformat()
    metadata_files = {
        "folders.jsonl": _jsonl_bytes(folders),
        "entries.jsonl": _jsonl_bytes(entries),
        "catalogs.jsonl": _jsonl_bytes(catalogs),
        "views.jsonl": _jsonl_bytes(views),
        "tags.jsonl": _jsonl_bytes(tags),
        "tag_aliases.jsonl": _jsonl_bytes(tag_aliases),
        "relations.jsonl": _jsonl_bytes(relations),
        "sessions.jsonl": _jsonl_bytes(sessions),
        "conversations.jsonl": _jsonl_bytes(conversations),
        "journals.jsonl": _jsonl_bytes(journals),
    }
    manifest = {
        "format": "marginalia-knowledge-pack",
        "schema_version": PACK_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "library_id": library_id,
        "app_version": __version__,
        "generator": {
            "name": "marginalia",
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "counts": {
            "folders": len(folders),
            "entries": len(entries),
            "catalogs": len(catalogs),
            "views": len(views),
            "tags": len(tags),
            "tag_aliases": len(tag_aliases),
            "relations": len(relations),
            "sessions": len(sessions),
            "conversations": len(conversations),
            "journals": len(journals),
            "blobs": len(blobs),
            "blob_bytes": sum(b.size_bytes for b in blobs),
        },
        "metadata_files": sorted(metadata_files),
        "blob_layout": "blobs/sha256/{first_two_hex}/{sha256}",
    }
    metadata_files = {
        "manifest.json": _json_bytes(manifest, indent=2),
        "README.md": _readme_bytes(manifest),
        **metadata_files,
    }
    return KnowledgePack(
        snapshot_id=snapshot_id,
        created_at=created_at,
        manifest=manifest,
        metadata_files=metadata_files,
        blobs=blobs,
    )


def _folder_record(row: Folder) -> dict[str, Any]:
    return {
        "folder_id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _catalog_record(row: Catalog) -> dict[str, Any]:
    return {
        "catalog_id": row.id,
        "parent_id": row.parent_id,
        "name": row.name,
        "summary": row.summary,
        "description": row.description,
        "extra": row.extra,
        "tags": row.tags,
        "is_system": row.is_system,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _view_record(row: View) -> dict[str, Any]:
    return {
        "view_id": row.id,
        "name": row.name,
        "summary": row.summary,
        "description": row.description,
        "extra": row.extra,
        "tags": row.tags,
        "filter_spec": row.filter_spec,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _tag_record(row: Tag) -> dict[str, Any]:
    return {
        "tag_id": row.id,
        "name": row.name,
        "facet": row.facet,
        "alias_of": row.alias_of,
        "doc_count": row.doc_count,
        "last_used_at": _dt(row.last_used_at),
        "last_reaffirmed_at": _dt(row.last_reaffirmed_at),
        "reaffirm_count": row.reaffirm_count,
        "created_at": _dt(row.created_at),
        "updated_at": _dt(row.updated_at),
    }


def _tag_alias_record(row: TagAlias) -> dict[str, Any]:
    return {
        "tag_alias_id": row.id,
        "from_name": row.from_name,
        "to_tag_id": row.to_tag_id,
        "note": row.note,
        "created_at": _dt(row.created_at),
    }


def _relation_record(row: EntryRelation) -> dict[str, Any]:
    return {
        "relation_id": row.id,
        "entry_a_id": row.entry_a_id,
        "entry_b_id": row.entry_b_id,
        "note": row.note,
        "source_kind": row.source_kind,
        "last_observed_at": _dt(row.last_observed_at),
        "observation_count": row.observation_count,
        "vetted": row.vetted,
        "vetted_reason": row.vetted_reason,
        "vetted_at": _dt(row.vetted_at),
        "vetted_observation_count": row.vetted_observation_count,
        "created_at": _dt(row.created_at),
    }


def _session_record(row: Session) -> dict[str, Any]:
    return {
        "session_id": row.id,
        "started_at": _dt(row.started_at),
        "ended_at": _dt(row.ended_at),
        "end_reason": row.end_reason,
        "deleted_at": _dt(row.deleted_at),
        "initiating_user_message": row.initiating_user_message,
        "turn_count": row.turn_count,
        "total_input_tokens": row.total_input_tokens,
        "total_output_tokens": row.total_output_tokens,
        "total_cache_read": row.total_cache_read,
        "total_tool_calls": row.total_tool_calls,
        "total_llm_calls": row.total_llm_calls,
        "total_cost_estimate": _decimal(row.total_cost_estimate),
        "total_duration_ms": row.total_duration_ms,
    }


def _conversation_record(row: Conversation) -> dict[str, Any]:
    return {
        "conversation_id": row.id,
        "session_id": row.session_id,
        "turn_index": row.turn_index,
        "started_at": _dt(row.started_at),
        "ended_at": _dt(row.ended_at),
        "user_message": row.user_message,
        "agent_response": row.agent_response,
        "tool_calls": row.tool_calls,
        "llm_calls": row.llm_calls,
        "total_input_tokens": row.total_input_tokens,
        "total_output_tokens": row.total_output_tokens,
        "total_cache_read": row.total_cache_read,
        "total_tool_calls": row.total_tool_calls,
        "total_llm_calls": row.total_llm_calls,
        "total_duration_ms": row.total_duration_ms,
        "total_cost_estimate": _decimal(row.total_cost_estimate),
    }


def _journal_record(row: Journal) -> dict[str, Any]:
    return {
        "journal_id": row.id,
        "conversation_id": row.conversation_id,
        "note": row.note,
        "entry_ids": row.entry_ids,
        "tags": row.tags,
        "source_kind": row.source_kind,
        "superseded_by_id": row.superseded_by_id,
        "summarized_journal_ids": row.summarized_journal_ids,
        "invalidated_at": _dt(row.invalidated_at),
        "invalidated_by_id": row.invalidated_by_id,
        "invalidated_reason": row.invalidated_reason,
        "created_at": _dt(row.created_at),
    }


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value: Decimal | None) -> str | None:
    # Cost accounting is deprecated (no pricing table exists), so a stored
    # 0 means "never computed" — export null instead of a fake "0". Real
    # non-zero values (e.g. imported from elsewhere) still round-trip.
    return str(value) if value else None


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=indent)
        + "\n"
    ).encode("utf-8")


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    return (
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows)
        + "\n"
    ).encode("utf-8")


def _readme_bytes(manifest: dict[str, Any]) -> bytes:
    counts = manifest["counts"]
    body = f"""# Marginalia Knowledge Pack

Snapshot: `{manifest["snapshot_id"]}`

Created: `{manifest["created_at"]}`

This folder is a portable Marginalia snapshot. `manifest.json` is the machine
entry point; `*.jsonl` files contain metadata; `blobs/` stores original files
by sha256. Do not treat this folder as a live database.

- entries: {counts["entries"]}
- folders: {counts["folders"]}
- tags: {counts["tags"]}
- sessions: {counts["sessions"]}
- conversations: {counts["conversations"]}
- journals: {counts["journals"]}
- blobs: {counts["blobs"]}
"""
    return body.encode("utf-8")
