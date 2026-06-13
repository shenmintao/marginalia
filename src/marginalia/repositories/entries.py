"""file_entries repository — pure SA queries against the FileEntry table
(and File joins where the join is part of the read shape).

Caller owns the transaction. Service-layer code should call these functions
instead of writing inline `select()` statements.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import (
    bindparam,
    cast,
    column,
    delete,
    func,
    literal_column,
    not_,
    or_,
    select,
    table,
    update,
    String,
    text as sa_text,
)
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.fts import ENTRY_METADATA_FTS_TABLE
from marginalia.db.models import EntryTag, File, FileEntry, TaskOutcome


ACTIVE_LIFECYCLES = ("active", "manual_active")
_MIN_TRIGRAM_FTS_TERM_LEN = 3
_ENTRY_METADATA_FTS = table(ENTRY_METADATA_FTS_TABLE, column("entry_id"))
_POSTGRES_TEXT_SEARCH_CONFIG = literal_column("'simple'")
_SQL_EMPTY_TEXT = literal_column("''")
_SQL_SPACE_TEXT = literal_column("' '")
_METADATA_FTS_STOPWORDS = {
    "about",
    "after",
    "against",
    "and",
    "are",
    "does",
    "from",
    "have",
    "into",
    "than",
    "that",
    "the",
    "their",
    "this",
    "with",
}


@dataclass(frozen=True, slots=True)
class _MetadataTextSearch:
    dialect: str
    fts_query: str
    like_terms: tuple[str, ...] = ()


def _folder_clause(folder_id: str | None):
    if folder_id is None:
        return FileEntry.folder_id.is_(None)
    return FileEntry.folder_id == folder_id


def _live_entry():
    """`FileEntry.deleted_at IS NULL` — 'entry has not been soft-deleted'."""
    return FileEntry.deleted_at.is_(None)


def _live_file():
    """`File.deleted_at IS NULL` — 'file row has not been soft-deleted'."""
    return File.deleted_at.is_(None)


def _apply_tag_filters(
    stmt,
    *,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
):
    """Glue tag-filter subqueries onto a select() over FileEntry."""
    for tid in tags_all or ():
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id == tid)
        stmt = stmt.where(FileEntry.id.in_(sub))
    if tags_any:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_any))
        stmt = stmt.where(FileEntry.id.in_(sub))
    if tags_none:
        sub = select(EntryTag.entry_id).where(EntryTag.tag_id.in_(tags_none))
        stmt = stmt.where(not_(FileEntry.id.in_(sub)))
    return stmt


def _text_terms(text: str | Sequence[str] | None) -> list[str]:
    if text is None:
        return []
    if isinstance(text, str):
        item = text.strip()
        return [item] if item else []
    return [str(item).strip() for item in text if str(item).strip()]


def _metadata_fts_query_from_terms(terms: list[str]) -> str | None:
    if not terms:
        return None
    cleaned = []
    for term in terms:
        clean = _clean_metadata_fts_term(term)
        if not clean:
            continue
        if clean.casefold() in _METADATA_FTS_STOPWORDS:
            continue
        if len(clean) < _MIN_TRIGRAM_FTS_TERM_LEN:
            continue
        cleaned.append(clean)
    if not cleaned:
        return None
    return " OR ".join(_quote_fts_phrase(term) for term in cleaned if term)


def _clean_metadata_fts_term(term: str) -> str:
    return term.replace("\x00", " ").strip(" \t\r\n.,;:!?()[]{}\"'")


def _metadata_like_terms(terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = _clean_metadata_fts_term(term)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _metadata_short_cjk_like_terms(terms: list[str]) -> list[str]:
    return [
        term
        for term in _metadata_like_terms(terms)
        if len(term) < _MIN_TRIGRAM_FTS_TERM_LEN and _contains_cjk(term)
    ]


def _contains_cjk(term: str) -> bool:
    return any(
        "\u3400" <= ch <= "\u4dbf"
        or "\u4e00" <= ch <= "\u9fff"
        or "\uf900" <= ch <= "\ufaff"
        for ch in term
    )


def _quote_fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def _metadata_like_clauses(terms: Sequence[str]):
    clauses = []
    for term in _metadata_like_terms(list(terms)):
        like = f"%{term}%"
        clauses.extend((
            File.summary.ilike(like),
            cast(File.description, String).ilike(like),
            File.extra.ilike(like),
            File.original_ext.ilike(like),
            FileEntry.extra.ilike(like),
            FileEntry.display_name.ilike(like),
        ))
    return clauses


async def _metadata_fts_query(
    db: AsyncSession, text: str | Sequence[str] | None,
) -> _MetadataTextSearch | None:
    terms = _text_terms(text)
    query = _metadata_fts_query_from_terms(terms)
    if query is None:
        return None
    if db.bind is None:
        return None
    dialect = db.bind.dialect.name
    like_terms = tuple(_metadata_short_cjk_like_terms(terms))
    if dialect == "postgresql":
        return _MetadataTextSearch(dialect="postgresql", fts_query=query, like_terms=like_terms)
    if dialect != "sqlite":
        return None
    try:
        exists = (
            await db.execute(
                sa_text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = :name"
                ),
                {"name": ENTRY_METADATA_FTS_TABLE},
            )
        ).scalar_one_or_none()
    except Exception:
        return None
    return (
        _MetadataTextSearch(dialect="sqlite", fts_query=query, like_terms=like_terms)
        if exists
        else None
    )


def _apply_metadata_fts_filter(stmt, search: _MetadataTextSearch):
    return stmt.where(_metadata_fts_filter_clause(search))


def _apply_metadata_fts_join(stmt, search: _MetadataTextSearch):
    if search.dialect == "sqlite":
        stmt = stmt.join(_ENTRY_METADATA_FTS, _ENTRY_METADATA_FTS.c.entry_id == FileEntry.id)
    return stmt.where(_metadata_fts_join_clause(search))


def _metadata_fts_ordering(search: _MetadataTextSearch):
    rank = _metadata_fts_rank(search)
    if search.dialect == "postgresql":
        return rank.desc()
    return rank


def _sqlite_mixed_fts_like_search(search: _MetadataTextSearch) -> bool:
    return search.dialect == "sqlite" and bool(search.like_terms)


def _metadata_fts_rank(search: _MetadataTextSearch):
    if search.dialect == "postgresql":
        tsquery = _postgres_metadata_tsquery(search)
        return (
            func.ts_rank_cd(_postgres_entry_metadata_tsvector(), tsquery)
            + func.ts_rank_cd(_postgres_file_metadata_tsvector(), tsquery)
        )
    return literal_column(f"bm25({ENTRY_METADATA_FTS_TABLE})")


def _metadata_fts_filter_clause(search: _MetadataTextSearch):
    clauses = []
    if search.dialect == "sqlite":
        clauses.append(FileEntry.id.in_(
            select(_ENTRY_METADATA_FTS.c.entry_id).where(_sqlite_metadata_match(search))
        ))
    elif search.dialect == "postgresql":
        clauses.append(_postgres_metadata_match(search))
    clauses.extend(_metadata_like_clauses(search.like_terms))
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


def _metadata_fts_join_clause(search: _MetadataTextSearch):
    clauses = []
    if search.dialect == "sqlite":
        clauses.append(_sqlite_metadata_match(search))
    elif search.dialect == "postgresql":
        clauses.append(_postgres_metadata_match(search))
    clauses.extend(_metadata_like_clauses(search.like_terms))
    return or_(*clauses) if len(clauses) > 1 else clauses[0]


def _sqlite_metadata_match(search: _MetadataTextSearch):
    return literal_column(ENTRY_METADATA_FTS_TABLE).op("MATCH")(
        bindparam("metadata_fts_query", search.fts_query)
    )


def _postgres_metadata_match(search: _MetadataTextSearch):
    tsquery = _postgres_metadata_tsquery(search)
    return or_(
        _postgres_entry_metadata_tsvector().op("@@")(tsquery),
        _postgres_file_metadata_tsvector().op("@@")(tsquery),
    )


def _postgres_metadata_tsquery(search: _MetadataTextSearch):
    return func.websearch_to_tsquery(
        _POSTGRES_TEXT_SEARCH_CONFIG,
        bindparam("metadata_fts_query", search.fts_query),
    )


def _postgres_entry_metadata_tsvector():
    return func.to_tsvector(
        _POSTGRES_TEXT_SEARCH_CONFIG,
        _concat_text(FileEntry.display_name, FileEntry.extra),
    )


def _postgres_file_metadata_tsvector():
    return func.to_tsvector(
        _POSTGRES_TEXT_SEARCH_CONFIG,
        _concat_text(File.summary, cast(File.description, String), File.extra, File.original_ext),
    )


def _concat_text(*values):
    expr = func.coalesce(cast(values[0], String), _SQL_EMPTY_TEXT)
    for value in values[1:]:
        expr = expr + _SQL_SPACE_TEXT + func.coalesce(cast(value, String), _SQL_EMPTY_TEXT)
    return expr


async def list_live_in_folder(
    db: AsyncSession, folder_id: str | None,
    *, limit: int | None = None, offset: int = 0,
) -> list[tuple[FileEntry, str | None]]:
    """Live entries directly under one folder + their `File.ingest_status`,
    ordered by display_name. Used by routes_folders for the GUI's folder
    listing — surfacing ingest_status lets the row paint a "failed" badge
    without a second round-trip. folder_id=None returns root entries.

    `limit` and `offset` are optional. The GUI route still calls this
    without them (it streams the whole list to the browser); the agent
    tool passes both to honor the pagination contract."""
    stmt = (
        select(FileEntry, File.ingest_status)
        .join(File, File.id == FileEntry.file_id)
        .where(
            _folder_clause(folder_id),
            _live_entry(),
        )
        .order_by(FileEntry.display_name)
    )
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return [(e, status) for e, status in rows]


async def count_live_in_folder(
    db: AsyncSession, folder_id: str | None,
) -> int:
    """Total live entries directly under `folder_id`. Pair with the
    paginated list_live_in_folder for the agent's `total` field."""
    stmt = (
        select(func.count())
        .select_from(FileEntry)
        .join(File, File.id == FileEntry.file_id)
        .where(_folder_clause(folder_id), _live_entry())
    )
    return int((await db.execute(stmt)).scalar_one())


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
                _live_entry(),
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
            .where(FileEntry.file_id == file_id, _live_entry())
            .order_by(FileEntry.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def search_with_file(
    db: AsyncSession, *, like: str, limit: int,
) -> list[tuple[FileEntry, File]]:
    """Free-text search across display_name, file.summary, file.original_ext.
    Returned rows are joined live-entries + their file rows, ordered by recency."""
    stmt = (
        select(FileEntry, File)
        .join(File, File.id == FileEntry.file_id)
        .where(
            _live_entry(),
            _live_file(),
        )
    )
    metadata_search = await _metadata_fts_query(db, like.strip("%"))
    if metadata_search is not None:
        stmt = _apply_metadata_fts_filter(stmt, metadata_search)
    else:
        clauses = _metadata_like_clauses([like.strip("%")])
        if clauses:
            stmt = stmt.where(or_(*clauses))
    rows = (
        await db.execute(
            stmt.order_by(FileEntry.updated_at.desc()).limit(limit)
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
                _live_entry(),
                _live_file(),
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
                _live_entry(),
                _live_file(),
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
                _live_entry(),
                _live_file(),
            )
            .order_by(FileEntry.folder_id, FileEntry.display_name)
        )
    ).all()
    return [(e, f) for e, f in rows]


def _build_filtered_stmt(
    *,
    text: str | Sequence[str] | None,
    lifecycle: list[str] | None,
    kind: str | None,
    catalog_one: str | None,
    catalog_in: list[str] | None,
    folder_one: str | None,
    folder_in: list[str] | None,
    tags_all: list[str] | None,
    tags_any: list[str] | None,
    tags_none: list[str] | None,
    extra_entry_ids: list[str] | None,
    base,
):
    """Shared WHERE-clause builder for search_filtered + count_filtered.

    Returns (stmt, empty_short_circuit). When `empty_short_circuit` is
    True the caller should skip executing and return zero results — this
    happens when an `..._in` filter is an empty list (semantically: 'no
    matches possible').
    """
    stmt = base.where(_live_entry(), _live_file())
    if lifecycle:
        stmt = stmt.where(FileEntry.lifecycle.in_(lifecycle))
    if kind:
        stmt = stmt.where(File.kind == kind)
    text_terms = _metadata_like_terms(_text_terms(text))
    if text_terms:
        clauses = _metadata_like_clauses(text_terms)
        if clauses:
            stmt = stmt.where(or_(*clauses))
    if catalog_one is not None:
        stmt = stmt.where(FileEntry.catalog_id == catalog_one)
    elif catalog_in is not None:
        if not catalog_in:
            return stmt, True
        stmt = stmt.where(FileEntry.catalog_id.in_(catalog_in))
    if folder_one is not None:
        stmt = stmt.where(FileEntry.folder_id == folder_one)
    elif folder_in is not None:
        if not folder_in:
            return stmt, True
        stmt = stmt.where(FileEntry.folder_id.in_(folder_in))
    stmt = _apply_tag_filters(
        stmt, tags_all=tags_all, tags_any=tags_any, tags_none=tags_none,
    )
    if extra_entry_ids is not None:
        if not extra_entry_ids:
            return stmt, True
        stmt = stmt.where(FileEntry.id.in_(extra_entry_ids))
    return stmt, False


async def search_filtered(
    db: AsyncSession,
    *,
    text: str | Sequence[str] | None = None,
    lifecycle: list[str] | None = None,
    kind: str | None = None,
    catalog_one: str | None = None,
    catalog_in: list[str] | None = None,
    folder_one: str | None = None,
    folder_in: list[str] | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    extra_entry_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[tuple[FileEntry, File]]:
    """The unified entry search used by `search_metadata` (agent tool) and
    `materialize_view`. All filters are conjunctive; an unset filter is a
    no-op. tag filters resolve through EntryTag subqueries. Pair with
    `count_filtered` for total-count pagination."""
    metadata_search = await _metadata_fts_query(db, text)
    base = select(FileEntry, File).join(File, File.id == FileEntry.file_id)
    stmt, empty = _build_filtered_stmt(
        text=None if metadata_search is not None else text,
        lifecycle=lifecycle, kind=kind,
        catalog_one=catalog_one, catalog_in=catalog_in,
        folder_one=folder_one, folder_in=folder_in,
        tags_all=tags_all, tags_any=tags_any, tags_none=tags_none,
        extra_entry_ids=extra_entry_ids, base=base,
    )
    if empty:
        return []
    if metadata_search is not None:
        if _sqlite_mixed_fts_like_search(metadata_search):
            stmt = _apply_metadata_fts_filter(stmt, metadata_search)
            stmt = stmt.order_by(FileEntry.updated_at.desc())
        else:
            stmt = _apply_metadata_fts_join(stmt, metadata_search)
            stmt = stmt.order_by(
                _metadata_fts_ordering(metadata_search),
                FileEntry.updated_at.desc(),
            )
    else:
        stmt = stmt.order_by(FileEntry.updated_at.desc())
    if offset:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    return [(e, f) for e, f in rows]


async def count_filtered(
    db: AsyncSession,
    *,
    text: str | Sequence[str] | None = None,
    lifecycle: list[str] | None = None,
    kind: str | None = None,
    catalog_one: str | None = None,
    catalog_in: list[str] | None = None,
    folder_one: str | None = None,
    folder_in: list[str] | None = None,
    tags_all: list[str] | None = None,
    tags_any: list[str] | None = None,
    tags_none: list[str] | None = None,
    extra_entry_ids: list[str] | None = None,
) -> int:
    """Total rows that would match `search_filtered` with the same filters,
    ignoring limit/offset. Used to populate `total` / `has_more` in the
    pagination contract."""
    metadata_search = await _metadata_fts_query(db, text)
    base = select(func.count()).select_from(
        FileEntry.__table__.join(File.__table__, File.id == FileEntry.file_id)
    )
    stmt, empty = _build_filtered_stmt(
        text=None if metadata_search is not None else text,
        lifecycle=lifecycle, kind=kind,
        catalog_one=catalog_one, catalog_in=catalog_in,
        folder_one=folder_one, folder_in=folder_in,
        tags_all=tags_all, tags_any=tags_any, tags_none=tags_none,
        extra_entry_ids=extra_entry_ids, base=base,
    )
    if empty:
        return 0
    if metadata_search is not None:
        stmt = _apply_metadata_fts_filter(stmt, metadata_search)
    return int((await db.execute(stmt)).scalar_one())


async def list_ids_under_filter_spec(
    db: AsyncSession,
    spec: dict[str, Any],
    *,
    default_lifecycle: list[str],
    catalog_subtree_expander,
) -> list[str]:
    """Evaluate a view's filter_spec and return matching entry_ids.
    `catalog_subtree_expander(root_id) -> list[str]` is injected so this
    repo doesn't import the catalogs repo (and breaks no layering)."""
    stmt = (
        select(FileEntry.id)
        .where(_live_entry())
        .where(FileEntry.lifecycle.in_(spec.get("lifecycle") or default_lifecycle))
    )
    sub = spec.get("catalog_subtree") or []
    if sub:
        ids: list[str] = []
        for r in sub:
            ids.extend(await catalog_subtree_expander(r))
        if not ids:
            return []
        stmt = stmt.where(FileEntry.catalog_id.in_(ids))
    stmt = _apply_tag_filters(
        stmt,
        tags_all=spec.get("tags_all"),
        tags_any=spec.get("tags_any"),
        tags_none=spec.get("tags_none"),
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_with_file_by_ids_any(
    db: AsyncSession, entry_ids: list[str],
) -> list[tuple[FileEntry, File]]:
    """Entries + files for the given ids, no deleted_at filter. The
    read_files tool wants to differentiate "not found" from "soft-deleted",
    so it can't use the live-only flavour."""
    if not entry_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(FileEntry.id.in_(entry_ids))
        )
    ).all()
    return [(e, f) for e, f in rows]


# uuid4 form: 8-4-4-4-12 hex with dashes
_UUID_LEN = 36
# Minimum prefix length we'll accept for short-id resolution. 8 chars of
# hex = 32 bits ≈ collision-free at any realistic library size, and it's
# the same prefix we display in the activity bar so what the user sees
# round-trips cleanly.
_MIN_PREFIX = 8


async def resolve_entry_id_prefix(
    db: AsyncSession, raw: str,
) -> tuple[str, str | None]:
    """Resolve a user-supplied entry_id to a full uuid.

    Accepts:
      - a full uuid (returned unchanged)
      - a short hex prefix (>= 8 chars, dashes optional) — promoted to the
        full id when exactly one entry has that prefix.

    Returns `(full_id, error)`. On success `error is None`. On failure
    `full_id` is the original input and `error` names the failure mode
    so the caller can surface it back to the agent.

    Soft-deleted entries are NOT excluded — same scope as
    `list_with_file_by_ids_any`, so the read_files / read_entries_metadata
    tools can still report ingest_status / lifecycle on a soft-deleted row.
    """
    s = (raw or "").strip()
    if not s:
        return raw, "missing entry_id"

    if len(s) == _UUID_LEN and s.count("-") == 4:
        return s, None

    cleaned = s.replace("-", "").lower()
    if len(cleaned) < _MIN_PREFIX or not all(
        c in "0123456789abcdef" for c in cleaned
    ):
        # Looks like a file name. Probe display_name to give the agent
        # back the actual id(s) so it can self-correct in one round-trip.
        candidates = (
            await db.execute(
                select(FileEntry.id, FileEntry.display_name)
                .where(FileEntry.display_name.ilike(f"%{s}%"))
                .where(FileEntry.lifecycle != "deleted")
                .limit(5)
            )
        ).all()
        if candidates:
            hint = "; ".join(
                f"{cid[:8]}={name!r}" for cid, name in candidates
            )
            return s, (
                f"entry_id={s!r} looks like a file name, not an id. "
                f"Likely matches (use the 8-char prefix or full id): "
                f"{hint}"
            )
        return s, (
            f"entry_id={s!r} is not a valid uuid or short prefix "
            f"(need >= {_MIN_PREFIX} hex chars). "
            "Use search_metadata or list_folder to get the id first."
        )

    # SQL LIKE on the de-dashed prefix. Entry ids are stored as 36-char
    # uuids with dashes, so we strip dashes from both sides via REPLACE.
    # Collation is fine because uuid hex is ASCII.
    rows = (
        await db.execute(
            select(FileEntry.id).where(
                FileEntry.id.ilike(f"{cleaned[:8]}%")
            ).limit(5)
        )
    ).scalars().all()

    # `cleaned[:8]` matches the leading hex of any uuid sharing that
    # prefix because uuid4 dashes always sit at fixed positions
    # (8/13/18/23). If the agent supplied more than 8 chars, narrow
    # further by comparing the full cleaned prefix against the de-dashed
    # row id in Python — saves us a second SQL function call.
    if len(cleaned) > 8:
        rows = [
            rid for rid in rows if rid.replace("-", "").lower().startswith(cleaned)
        ]

    if not rows:
        return s, f"no entry matches prefix {s!r}"
    if len(rows) > 1:
        sample = ", ".join(r[:8] for r in rows[:3])
        return s, (
            f"prefix {s!r} is ambiguous ({len(rows)} matches: {sample}); "
            "supply more characters of the id."
        )
    return rows[0], None


async def list_by_ids_any(
    db: AsyncSession, entry_ids: list[str],
) -> list[FileEntry]:
    """Entries (no File join, no deleted filter) for the given ids."""
    if not entry_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry).where(FileEntry.id.in_(entry_ids))
        )
    ).scalars().all()
    return list(rows)


async def list_journal_reference_statuses(
    db: AsyncSession, entry_ids: list[str],
) -> dict[str, dict[str, datetime | None]]:
    """Return liveness/freshness fields for journal `entry_ids`.

    Journal rows deliberately keep historical references in JSON. Recall-time
    validation needs to distinguish current references from missing,
    soft-deleted, or reprocessed sources without filtering the journal row out.
    """
    if not entry_ids:
        return {}
    rows = (
        await db.execute(
            select(
                FileEntry.id,
                FileEntry.deleted_at,
                File.deleted_at,
                File.ingested_at,
            )
            .join(File, File.id == FileEntry.file_id)
            .where(FileEntry.id.in_(entry_ids))
        )
    ).all()
    return {
        entry_id: {
            "entry_deleted_at": entry_deleted_at,
            "file_deleted_at": file_deleted_at,
            "file_ingested_at": file_ingested_at,
        }
        for entry_id, entry_deleted_at, file_deleted_at, file_ingested_at in rows
    }


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
                _live_entry(),
                _live_file(),
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
                _live_entry(),
            )
        )
    ).scalars().all()
    return list(rows)


async def list_purge_due(
    db: AsyncSession, now: datetime,
) -> list[FileEntry]:
    """Soft-deleted entries past their purge_after timestamp. Used by the
    purge_deleted_files handler — it physically deletes them."""
    rows = (
        await db.execute(
            select(FileEntry).where(
                FileEntry.deleted_at.isnot(None),
                FileEntry.purge_after.isnot(None),
                FileEntry.purge_after < now,
            )
        )
    ).scalars().all()
    return list(rows)


async def hard_delete_by_id(db: AsyncSession, entry_id: str) -> None:
    """Physical delete (FK CASCADE clears entry_tags). Used by the purge
    handler — this is the only legal path for a hard entry delete."""
    await db.execute(delete(FileEntry).where(FileEntry.id == entry_id))


async def has_live_entry_for_file(
    db: AsyncSession, file_id: str,
) -> bool:
    """True if any live entry still references `file_id`. Used by the purge
    handler to decide whether to drop the file row + storage object."""
    row = (
        await db.execute(
            select(FileEntry.id).where(
                FileEntry.file_id == file_id,
                _live_entry(),
            ).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def has_any_entry_for_file(
    db: AsyncSession, file_id: str,
) -> bool:
    """True if any entry (live or soft-deleted) still references `file_id`."""
    row = (
        await db.execute(
            select(FileEntry.id).where(FileEntry.file_id == file_id).limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


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


async def list_live_active_with_file(
    db: AsyncSession,
) -> list[tuple[FileEntry, File]]:
    """Live entries with lifecycle in {active, manual_active} joined to their
    live file rows."""
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                _live_entry(),
                _live_file(),
                FileEntry.lifecycle.in_(ACTIVE_LIFECYCLES),
            )
        )
    ).all()
    return [(e, f) for e, f in rows]


async def list_active_with_file_by_ids(
    db: AsyncSession, entry_ids: list[str],
) -> list[tuple[FileEntry, File]]:
    """Live entries with lifecycle in {active, manual_active} joined to their
    file rows, restricted to `entry_ids`. Used by refresh_entry_extra to
    resolve a candidate set produced from journal mentions."""
    if not entry_ids:
        return []
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.id.in_(entry_ids),
                _live_entry(),
                FileEntry.lifecycle.in_(ACTIVE_LIFECYCLES),
            )
        )
    ).all()
    return [(e, f) for e, f in rows]


async def list_active_recent_updated(
    db: AsyncSession, *, limit: int,
) -> list[FileEntry]:
    """Top-N most-recently-updated active entries (no File join). Used by
    restructure_catalogs to feed the LLM a high-activity sample."""
    rows = (
        await db.execute(
            select(FileEntry)
            .where(
                FileEntry.lifecycle.in_(ACTIVE_LIFECYCLES),
                _live_entry(),
            )
            .order_by(FileEntry.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def list_active_with_file_eligible_for_enrich(
    db: AsyncSession, *, recent_cutoff: datetime, limit: int,
) -> list[tuple[FileEntry, File]]:
    """Active live entries whose file is ingest_status='done' and which were
    NOT recently enriched (no task_outcomes row with task_kind='enrich_tags',
    object_kind='file_entry', completed_at >= recent_cutoff). Ordered by
    created_at asc. Used by enrich_tags."""
    recently = (
        select(TaskOutcome.object_id)
        .where(
            TaskOutcome.task_kind == "enrich_tags",
            TaskOutcome.object_kind == "file_entry",
            TaskOutcome.completed_at >= recent_cutoff,
        )
    ).subquery()
    rows = (
        await db.execute(
            select(FileEntry, File)
            .join(File, File.id == FileEntry.file_id)
            .where(
                FileEntry.lifecycle.in_(ACTIVE_LIFECYCLES),
                _live_entry(),
                File.ingest_status == "done",
                _live_file(),
                not_(FileEntry.id.in_(select(recently.c.object_id))),
            )
            .order_by(FileEntry.created_at.asc())
            .limit(limit)
        )
    ).all()
    return [(e, f) for e, f in rows]


async def list_active_for_demotion(
    db: AsyncSession, *, cutoff_age: datetime,
) -> list[tuple[str, datetime]]:
    """`(entry_id, created_at)` for live active entries created at or before
    `cutoff_age`, oldest first. Used by suggest_lifecycle's demote phase."""
    rows = (
        await db.execute(
            select(FileEntry.id, FileEntry.created_at)
            .where(
                FileEntry.lifecycle == "active",
                _live_entry(),
                FileEntry.created_at <= cutoff_age,
            )
            .order_by(FileEntry.created_at.asc())
        )
    ).all()
    return [(eid, ca) for eid, ca in rows]


async def list_demoted_for_archive(
    db: AsyncSession, *, cutoff_demoted: datetime,
) -> list[tuple[str, datetime]]:
    """`(entry_id, updated_at)` for live demoted entries last updated at or
    before `cutoff_demoted`, oldest first. Used by suggest_lifecycle's
    archive phase."""
    rows = (
        await db.execute(
            select(FileEntry.id, FileEntry.updated_at)
            .where(
                FileEntry.lifecycle == "demoted",
                _live_entry(),
                FileEntry.updated_at <= cutoff_demoted,
            )
            .order_by(FileEntry.updated_at.asc())
        )
    ).all()
    return [(eid, ua) for eid, ua in rows]


async def transition_lifecycle(
    db: AsyncSession,
    *,
    entry_id: str,
    from_lifecycle: str,
    to_lifecycle: str,
    now: datetime,
) -> int:
    """Guarded lifecycle transition: only fires if the row is still in
    `from_lifecycle` and not soft-deleted. Returns rowcount so the caller
    can detect a race (rowcount=0 means the row's state changed first)."""
    result = await db.execute(
        update(FileEntry)
        .where(
            FileEntry.id == entry_id,
            FileEntry.lifecycle == from_lifecycle,
            _live_entry(),
        )
        .values(lifecycle=to_lifecycle, updated_at=now)
    )
    return int(result.rowcount or 0)


async def update_extra(
    db: AsyncSession, *, entry_id: str, extra: str | None, now: datetime,
) -> None:
    """Set `entry.extra` and bump updated_at. Used by refresh_entry_extra."""
    await db.execute(
        update(FileEntry)
        .where(FileEntry.id == entry_id)
        .values(extra=extra, updated_at=now)
    )


async def list_sibling_display_names(
    db: AsyncSession, *, folder_id: str | None, exclude_entry_id: str,
) -> list[str]:
    """Display names of every other live entry in the same folder, ordered
    by name. Used by ingest_file's pipeline-context builder."""
    rows = (
        await db.execute(
            select(FileEntry.display_name)
            .where(
                _folder_clause(folder_id),
                FileEntry.id != exclude_entry_id,
                _live_entry(),
            )
            .order_by(FileEntry.display_name)
        )
    ).scalars().all()
    return list(rows)


async def find_first_live_for_file(
    db: AsyncSession, file_id: str,
) -> FileEntry | None:
    """Oldest live entry pointing at `file_id`. Used by ingest_file to pick
    the entry whose AI fields will be filled."""
    return (
        await db.execute(
            select(FileEntry)
            .where(FileEntry.file_id == file_id, _live_entry())
            .order_by(FileEntry.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def find_first_display_name_for_file(
    db: AsyncSession, file_id: str,
) -> str | None:
    """Display name of the oldest entry pointing at `file_id` (live or
    soft-deleted). Used by pipelines.archive to pick a stable filename hint
    for py7zz."""
    row = (
        await db.execute(
            select(FileEntry.display_name)
            .where(FileEntry.file_id == file_id)
            .order_by(FileEntry.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return row
