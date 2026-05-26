"""files repository — pure SA queries against the File table.

Caller owns the transaction.
"""
from __future__ import annotations

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry


async def get_by_sha256(db: AsyncSession, sha256: str) -> File | None:
    """Live or soft-deleted file row matching the content hash. Used by
    upload to detect dedup hits before a tentative storage put is finalised."""
    return (
        await db.execute(select(File).where(File.sha256 == sha256))
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


async def list_live_ids(db: AsyncSession) -> list[str]:
    """Every live file id, oldest first. Used by reprocess `all=true`."""
    rows = (
        await db.execute(
            select(File.id)
            .where(File.deleted_at.is_(None))
            .order_by(File.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_in_catalogs(
    db: AsyncSession, catalog_ids: list[str],
) -> list[str]:
    """Distinct live file ids whose live entries sit in any of the given
    catalogs. Used by reprocess catalog-subtree filter — caller expands
    the subtree via catalogs_repo.expand_subtree first."""
    if not catalog_ids:
        return []
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .where(
                FileEntry.catalog_id.in_(catalog_ids),
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_in_folders(
    db: AsyncSession, folder_ids: list[str],
) -> list[str]:
    """Distinct live file ids whose live entries sit in any of the given
    folders. Used by reprocess folder-scoped filter — caller has already
    walked any folder subtree."""
    if not folder_ids:
        return []
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .where(
                FileEntry.folder_id.in_(folder_ids),
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
            .distinct()
        )
    ).scalars().all()
    return list(rows)


async def list_live_ids_with_tag(
    db: AsyncSession, tag_id: str,
) -> list[str]:
    """Distinct live file ids whose live entries carry `tag_id`. Used by
    reprocess tag filter."""
    from marginalia.db.models import EntryTag  # local — keep top imports tight
    rows = (
        await db.execute(
            select(File.id)
            .join(FileEntry, FileEntry.file_id == File.id)
            .join(EntryTag, EntryTag.entry_id == FileEntry.id)
            .where(
                EntryTag.tag_id == tag_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
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
