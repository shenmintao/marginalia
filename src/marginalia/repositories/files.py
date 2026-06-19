"""files repository — pure SA queries against the File table.

Caller owns the transaction.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry


async def get_by_sha256(db: AsyncSession, sha256: str) -> File | None:
    """Live or soft-deleted file row matching the content hash. Used by
    upload to detect dedup hits before a tentative storage put is finalised."""
    return (
        await db.execute(
            select(File)
            .where(File.sha256 == sha256)
            .order_by(File.deleted_at.isnot(None), File.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def hard_delete_by_id(db: AsyncSession, file_id: str) -> None:
    """Physical delete of a file row. Used by purge_deleted_files when no
    live or soft-deleted entries still reference the file."""
    await db.execute(delete(File).where(File.id == file_id))


async def list_live_storage_keys(
    db: AsyncSession,
) -> list[tuple[str, str, str]]:
    """`(id, storage_key, sha256)` for every live file row, oldest first.
    Used by the storage migration CLI to walk every file."""
    rows = (
        await db.execute(
            select(File.id, File.storage_key, File.sha256)
            .where(File.deleted_at.is_(None))
            .order_by(File.created_at.asc())
        )
    ).all()
    return [(fid, sk, sha) for fid, sk, sha in rows]


async def sample_live_storage_keys(
    db: AsyncSession, *, limit: int,
) -> list[str]:
    """First N live storage_keys. Used by the startup consistency check that
    detects backend-vs-stored-key mismatches."""
    rows = (
        await db.execute(
            select(File.storage_key)
            .where(File.deleted_at.is_(None))
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def update_storage_key(
    db: AsyncSession, *, file_id: str, storage_key: str,
) -> None:
    """Used by the storage migration CLI after successfully writing the
    file body to its new key on the destination backend."""
    await db.execute(
        update(File)
        .where(File.id == file_id)
        .values(storage_key=storage_key)
    )


async def mark_ingest_failed(
    db: AsyncSession, *, file_id: str, now: datetime,
) -> bool:
    """Mark a live file as failed if ingest has not completed.

    Used when an ingest_file task reaches the terminal dead state outside the
    handler's normal exception path, e.g. stale-task recovery or a runner-level
    configuration failure.
    """
    result = await db.execute(
        update(File)
        .where(
            File.id == file_id,
            File.deleted_at.is_(None),
            File.ingest_status.in_(("pending", "processing")),
        )
        .values(ingest_status="failed", updated_at=now)
    )
    return bool(result.rowcount or 0)


async def list_live_ids(
    db: AsyncSession, *, ingest_status: str | None = None,
) -> list[str]:
    """Every live file id, oldest first. Used by reprocess `all=true`."""
    conditions = [File.deleted_at.is_(None)]
    if ingest_status is not None:
        conditions.append(File.ingest_status == ingest_status)
    rows = (
        await db.execute(
            select(File.id)
            .where(*conditions)
            .order_by(File.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_in_catalogs(
    db: AsyncSession,
    catalog_ids: list[str],
    *,
    ingest_status: str | None = None,
) -> list[str]:
    """Distinct live file ids whose live entries sit in any of the given
    catalogs. Used by reprocess catalog-subtree filter — caller expands
    the subtree via catalogs_repo.expand_subtree first."""
    if not catalog_ids:
        return []
    conditions = [
        FileEntry.catalog_id.in_(catalog_ids),
        FileEntry.deleted_at.is_(None),
        File.deleted_at.is_(None),
    ]
    if ingest_status is not None:
        conditions.append(File.ingest_status == ingest_status)
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .where(*conditions)
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_in_folders(
    db: AsyncSession,
    folder_ids: list[str],
    *,
    ingest_status: str | None = None,
) -> list[str]:
    """Distinct live file ids whose live entries sit in any of the given
    folders. Used by reprocess folder-scoped filter — caller has already
    walked any folder subtree."""
    if not folder_ids:
        return []
    conditions = [
        FileEntry.folder_id.in_(folder_ids),
        FileEntry.deleted_at.is_(None),
        File.deleted_at.is_(None),
    ]
    if ingest_status is not None:
        conditions.append(File.ingest_status == ingest_status)
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .where(*conditions)
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_with_tag(
    db: AsyncSession,
    tag_id: str,
    *,
    ingest_status: str | None = None,
) -> list[str]:
    """Distinct live file ids whose live entries carry `tag_id`. Used by
    reprocess tag filter."""
    from marginalia.db.models import EntryTag  # local — keep top imports tight
    conditions = [
        EntryTag.tag_id == tag_id,
        FileEntry.deleted_at.is_(None),
        File.deleted_at.is_(None),
    ]
    if ingest_status is not None:
        conditions.append(File.ingest_status == ingest_status)
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .join(EntryTag, EntryTag.entry_id == FileEntry.id)
            .where(*conditions)
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def list_live_entry_ids_for_file(
    db: AsyncSession, file_id: str,
) -> list[str]:
    """Live entry ids for the given file. Used by reprocess to know which
    entries need their entry_tags purged."""
    rows = (
        await db.execute(
            select(FileEntry.id).where(
                FileEntry.file_id == file_id,
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def find_low_quality(
    db: AsyncSession, *, min_summary_chars: int, limit: int,
) -> list[str]:
    """Live, already-ingested file ids whose summary is empty/whitespace.
    Oldest-ingested first so the periodic self-heal makes steady forward
    progress instead of thrashing the same recent file. Used by
    periodic_tick._dispatch_reprocess_low_quality.

    `ingested_at IS NOT NULL` filters out files mid-pipeline — those will
    set their summary on this run; we only want files that already
    finished without producing a usable summary.
    """
    rows = (
        await db.execute(
            select(File.id)
            .where(
                File.deleted_at.is_(None),
                File.ingested_at.is_not(None),
                or_(
                    File.summary.is_(None),
                    func.length(func.coalesce(func.trim(File.summary), "")) < min_summary_chars,
                ),
            )
            .order_by(File.ingested_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)
