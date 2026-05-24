"""file_entries repository — pure SA queries against the FileEntry table
(and File joins where the join is part of the read shape).

Caller owns the transaction. Service-layer code should call these functions
instead of writing inline `select()` statements.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import File, FileEntry


def _folder_clause(folder_id: str | None):
    if folder_id is None:
        return FileEntry.folder_id.is_(None)
    return FileEntry.folder_id == folder_id


async def find_live_by_folder_and_name(
    db: AsyncSession, folder_id: str | None, name: str,
) -> FileEntry | None:
    """Live entry matching `(folder_id, display_name)` — used by upload's
    name-conflict policy."""
    return (
        await db.execute(
            select(FileEntry).where(
                _folder_clause(folder_id),
                FileEntry.display_name == name,
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()


async def find_seed_by_file_id(
    db: AsyncSession, file_id: str,
) -> FileEntry | None:
    """Oldest live entry pointing at the given file — used by dedup to copy
    AI fields onto a new entry."""
    return (
        await db.execute(
            select(FileEntry)
            .where(FileEntry.file_id == file_id, FileEntry.deleted_at.is_(None))
            .order_by(FileEntry.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def search_with_file(
    db: AsyncSession, *, like: str, limit: int,
) -> list[tuple[FileEntry, File]]:
    """Free-text search across display_name, file.summary, file.original_ext.
    Returned rows are joined live-entries + their file rows, ordered by recency."""
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
                or_(
                    FileEntry.display_name.ilike(like),
                    File.summary.ilike(like),
                    File.original_ext.ilike(like),
                ),
            )
            .order_by(FileEntry.updated_at.desc())
            .limit(limit)
        )
    ).all()
    return [(e, f) for e, f in rows]


async def get_live_with_file(
    db: AsyncSession, entry_id: str,
) -> tuple[FileEntry, File] | None:
    """Live entry + its live file row, matching `entry_id`."""
    pair = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id == entry_id,
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).first()
    if pair is None:
        return None
    return pair[0], pair[1]


async def list_live_with_file(db: AsyncSession) -> list[tuple[FileEntry, File]]:
    """Every live entry + its live file row. Used by scan."""
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, FileEntry.file_id == File.id)
            .where(
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).all()
    return [(e, f) for e, f in rows]


async def list_live_with_file_in_folders(
    db: AsyncSession, folder_ids: list[str],
) -> list[tuple[FileEntry, File]]:
    """Live entries + their files for every entry whose folder_id is in
    `folder_ids`. Ordered by `(folder_id, display_name)` for stable zip
    layout. Empty list if `folder_ids` is empty."""
    if not folder_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.folder_id.in_(folder_ids),
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
            .order_by(FileEntry.folder_id, FileEntry.display_name)
        )
    ).all()
    return [(e, f) for e, f in rows]


async def list_live_with_file_by_ids(
    db: AsyncSession, entry_ids: list[str],
) -> list[tuple[FileEntry, File]]:
    """Live entries + files for the given ids. Used by exports to bulk-resolve
    citations."""
    if not entry_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id.in_(entry_ids),
                FileEntry.deleted_at.is_(None),
                File.deleted_at.is_(None),
            )
        )
    ).all()
    return [(e, f) for e, f in rows]


async def filter_live_ids(
    db: AsyncSession, candidate_ids: list[str],
) -> list[str]:
    """Of the candidate ids, keep only those whose entry is live."""
    if not candidate_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry.id).where(
                FileEntry.id.in_(candidate_ids),
                FileEntry.deleted_at.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


async def list_display_names(
    db: AsyncSession, entry_ids: list[str],
) -> dict[str, str]:
    """Return `{entry_id: display_name}` for the given ids (live or not).
    Used by recommend to label the random-walk results."""
    if not entry_ids:
        return {}
    rows = (
        await db.execute(
            select(FileEntry.id, FileEntry.display_name)
            .where(FileEntry.id.in_(entry_ids))
        )
    ).all()
    return {eid: name for eid, name in rows}
