"""search_metadata — DESIGN.md §10.1.

Filters entries by text terms (OR across display name, summary, and extras),
tags, catalog scope, folder scope, view, kind, and lifecycle. Catalog filters
(`catalog_id` / `catalog_subtree`) are mutually exclusive; folder filters
(`folder_id` / `folder_subtree`) are mutually exclusive. Catalog and folder
filters can be combined (AND). Returns minimal entry rows + pagination metadata
(`total`, `has_more`, `next_offset`).
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.text_query import normalize_text_queries
from marginalia.agent.tools import ToolContext, tool
from marginalia.db.models import View
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import folders as folders_repo


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "text": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": (
                "One query string or an array of query terms/phrases. Array "
                "items are ORed against display_name, files.summary, "
                "files.extra, and entry.extra. For multi-keyword recall, "
                "prefer an array instead of one space-joined string."
            ),
        },
        "tags_all": {"type": "array", "items": {"type": "string"}},
        "tags_any": {"type": "array", "items": {"type": "string"}},
        "tags_none": {"type": "array", "items": {"type": "string"}},
        "catalog_id": {
            "type": "string",
            "description": "Single catalog match. Mutually exclusive with catalog_subtree.",
        },
        "catalog_subtree": {
            "type": "string",
            "description": "Catalog id whose subtree (incl. self) the entry must fall in. Mutually exclusive with catalog_id.",
        },
        "folder_id": {
            "type": "string",
            "description": "Single folder match. Mutually exclusive with folder_subtree.",
        },
        "folder_subtree": {
            "type": "string",
            "description": "Folder id whose subtree (incl. self) the entry must fall in. Mutually exclusive with folder_id.",
        },
        "view_id": {
            "type": "string",
            "description": "Restrict to entries already inside this view's filter_spec.",
        },
        "kind": {"type": "string"},
        "lifecycle": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["active", "demoted", "archived", "manual_active", "manual_archived"],
            },
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "Skip first N matches (default 0). Use with `next_offset` to page.",
        },
    },
}


@tool(
    name="search_metadata",
    description=(
        "Low-level metadata filter for focused follow-up and narrow "
        "file/folder/catalog targets. Tag ids must be already "
        "resolved (use resolve_tag). For multi-keyword text recall, pass "
        "`text` as an array; text terms are ORed, then combined with tags, "
        "catalog, folder, lifecycle, and kind filters by AND. Catalog "
        "filters: catalog_id picks one node only; catalog_subtree picks the "
        "node and all descendants — "
        "mutually exclusive. Folder filters work the same way "
        "(folder_id / folder_subtree, mutually exclusive). Catalog and "
        "folder filters AND together. Pass `offset` (with the previous "
        "call's `next_offset`) to page beyond `limit`; the response carries "
        "`total` and `has_more`. Rows include compact coverage metadata when "
        "available."
    ),
    schema=SCHEMA,
)
async def search_metadata(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    text_q = normalize_text_queries(args.get("text")) or None
    tags_all = args.get("tags_all") or []
    tags_any = args.get("tags_any") or []
    tags_none = args.get("tags_none") or []
    cat_one = args.get("catalog_id")
    cat_subtree = args.get("catalog_subtree")
    fld_one = args.get("folder_id")
    fld_subtree = args.get("folder_subtree")
    view_id = args.get("view_id")
    kind = args.get("kind")
    lifecycle = args.get("lifecycle") or ["active", "manual_active"]
    limit = min(int(args.get("limit") or 50), 500)
    offset = max(0, int(args.get("offset") or 0))

    if cat_one and cat_subtree:
        return {"error": "catalog_id and catalog_subtree are mutually exclusive"}
    if fld_one and fld_subtree:
        return {"error": "folder_id and folder_subtree are mutually exclusive"}

    catalog_in: list[str] | None = None
    if cat_subtree:
        catalog_in = await catalogs_repo.expand_subtree(db, cat_subtree)
        if not catalog_in:
            return _empty_page(limit, offset)

    folder_in: list[str] | None = None
    if fld_subtree:
        folder_in = await folders_repo.expand_subtree(db, fld_subtree)
        if not folder_in:
            return _empty_page(limit, offset)

    extra_ids: list[str] | None = None
    if view_id:
        view = await db.get(View, view_id)
        if view is None:
            return {"error": "view not found", "view_id": view_id}
        spec = view.filter_spec or {}
        extra_ids = await entries_repo.list_ids_under_filter_spec(
            db, spec,
            default_lifecycle=lifecycle,
            catalog_subtree_expander=lambda rid: catalogs_repo.expand_subtree(db, rid),
        )
        if not extra_ids:
            return _empty_page(limit, offset)

    common_filters = dict(
        text=text_q,
        lifecycle=lifecycle,
        kind=kind,
        catalog_one=cat_one,
        catalog_in=catalog_in,
        folder_one=fld_one,
        folder_in=folder_in,
        tags_all=tags_all,
        tags_any=tags_any,
        tags_none=tags_none,
        extra_entry_ids=extra_ids,
    )

    total = await entries_repo.count_filtered(db, **common_filters)
    if _should_rerank(text_q, tags_all, tags_any):
        fetch_limit = min(total, max(limit + offset, min(500, total)))
        rows = await entries_repo.search_filtered(
            db, **common_filters, limit=fetch_limit, offset=0,
        )
        rows = await _rerank_rows(
            db,
            rows,
            text_terms=text_q or [],
            tags_all=tags_all,
            tags_any=tags_any,
        )
        rows = rows[offset: offset + limit]
    else:
        rows = await entries_repo.search_filtered(
            db, **common_filters, limit=limit, offset=offset,
        )

    has_more = (offset + len(rows)) < total
    out: dict[str, Any] = {
        "entries": [_entry_row(e, f) for e, f in rows],
        "count": len(rows),
        "total": total,
        "has_more": has_more,
    }
    if has_more:
        out["next_offset"] = offset + len(rows)
    return out


def _entry_row(entry: Any, file_row: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "entry_id": entry.id,
        "display_name": entry.display_name,
        "lifecycle": entry.lifecycle,
        "kind": file_row.kind,
        "summary": file_row.summary,
        "catalog_id": entry.catalog_id,
        "folder_id": entry.folder_id,
    }
    coverage = _compact_coverage(file_row.description)
    if coverage is not None:
        row["coverage"] = coverage
    return row


async def _rerank_rows(
    db: AsyncSession,
    rows: list[tuple[Any, Any]],
    *,
    text_terms: list[str],
    tags_all: list[str],
    tags_any: list[str],
) -> list[tuple[Any, Any]]:
    if not rows:
        return rows
    entry_ids = [entry.id for entry, _ in rows]
    tag_rows = await entry_tags_repo.list_id_name_facet_for_entries(db, entry_ids)
    tags_by_entry: dict[str, list[tuple[str, str, str | None]]] = {}
    for entry_id, tag_id, name, facet in tag_rows:
        tags_by_entry.setdefault(entry_id, []).append((tag_id, name, facet))

    query_terms = _rank_terms(text_terms)
    requested_tags = set(tags_all or []) | set(tags_any or [])
    scored: list[tuple[float, int, tuple[Any, Any]]] = []
    for idx, (entry, file_row) in enumerate(rows):
        score = _metadata_rank_score(
            entry=entry,
            file_row=file_row,
            query_terms=query_terms,
            requested_tags=requested_tags,
            entry_tags=tags_by_entry.get(entry.id, []),
        )
        scored.append((score, idx, (entry, file_row)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _score, _idx, row in scored]


def _should_rerank(
    text_q: list[str] | None,
    tags_all: list[str],
    tags_any: list[str],
) -> bool:
    return bool(text_q or tags_all or tags_any)


def _metadata_rank_score(
    *,
    entry: Any,
    file_row: Any,
    query_terms: list[str],
    requested_tags: set[str],
    entry_tags: list[tuple[str, str, str | None]],
) -> float:
    score = 0.0
    covered: set[str] = set()
    fields = [
        (entry.display_name, 22.0),
        (file_row.summary, 14.0),
        (file_row.extra, 10.0),
        (entry.extra, 10.0),
        (_description_text(file_row.description), 6.0),
        (file_row.original_ext, 1.0),
    ]
    for term in query_terms:
        term_score = 0.0
        for raw, weight in fields:
            hits = _term_hits(raw, term)
            if hits:
                term_score += weight * min(hits, 3)
        if term_score:
            covered.add(term.casefold())
            score += term_score * _term_weight(term)

    if query_terms:
        score += 5.0 * (len(covered) / len(query_terms))

    for tag_id, name, facet in entry_tags:
        if tag_id in requested_tags:
            score += _tag_facet_weight(facet) + 4.0
        for term in query_terms:
            if _term_hits(name, term):
                covered.add(term.casefold())
                score += _tag_facet_weight(facet) * _term_weight(term)
    return score


_RANK_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_./-]*")
_RANK_STOPWORDS = {
    "about",
    "after",
    "and",
    "are",
    "consisting",
    "does",
    "from",
    "have",
    "into",
    "larger",
    "than",
    "that",
    "the",
    "their",
    "this",
    "with",
}


def _rank_terms(text_terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in text_terms:
        text = str(raw or "").strip()
        if not text:
            continue
        candidates = [text]
        candidates.extend(_RANK_TOKEN_RE.findall(text))
        for candidate in candidates:
            term = candidate.strip(".,;:!?()[]{}\"'")
            if not term:
                continue
            key = term.casefold()
            has_digit = any(ch.isdigit() for ch in term)
            has_upper = any(ch.isupper() for ch in term)
            if key in seen or key in _RANK_STOPWORDS:
                continue
            if len(term) < 4 and not has_digit and not has_upper:
                continue
            seen.add(key)
            out.append(term)
    return out


def _term_hits(raw: Any, term: str) -> int:
    if raw is None:
        return 0
    haystack = _stringify(raw).casefold()
    needle = term.casefold()
    if not needle:
        return 0
    return haystack.count(needle)


def _term_weight(term: str) -> float:
    weight = 1.0
    if len(term) >= 7:
        weight += 0.5
    if any(ch.isdigit() for ch in term):
        weight += 1.0
    if any(ch.isupper() for ch in term):
        weight += 0.6
    if any(ch in term for ch in "/+-_."):
        weight += 0.4
    return weight


def _tag_facet_weight(facet: str | None) -> float:
    return {
        "topic": 8.0,
        "source": 5.0,
        "time": 4.0,
        "extra": 4.0,
        "form": 1.5,
        "language": 0.5,
    }.get(facet or "", 2.0)


def _description_text(description: Any) -> str:
    if isinstance(description, str):
        return description
    if not isinstance(description, dict):
        return ""
    parts: list[str] = []
    text = description.get("text")
    if isinstance(text, str):
        parts.append(text)
    sections = description.get("sections")
    if isinstance(sections, list):
        for section in sections[:50]:
            if not isinstance(section, dict):
                continue
            for key in ("title", "summary", "key_terms"):
                value = section.get(key)
                if value:
                    parts.append(_stringify(value))
    return "\n".join(parts)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_stringify(item) for item in value.values())
    return str(value)


def _compact_coverage(description: Any) -> dict[str, Any] | None:
    if not isinstance(description, dict):
        return None
    coverage = description.get("coverage")
    if not isinstance(coverage, dict):
        return None
    keys = (
        "unit",
        "total_pages",
        "indexed_pages",
        "total_units",
        "indexed_units",
        "total_bytes",
        "indexed_bytes",
        "total_chars",
        "indexed_chars",
        "total_lines",
        "indexed_lines",
        "total_rows",
        "indexed_rows",
        "indexed_partial",
        "partial_reasons",
        "source_mode",
        "max_index_pages",
        "max_index_bytes",
        "max_index_chars",
        "max_rows_per_sheet",
        "max_sample_lines",
        "sampled",
        "chunked",
        "chunk_count",
        "text_truncated",
        "truncated_chunks",
        "ocr_used",
        "ocr_pages_done",
    )
    compact = {key: coverage[key] for key in keys if key in coverage}
    return compact or None


def _empty_page(limit: int, offset: int) -> dict[str, Any]:
    return {
        "entries": [],
        "count": 0,
        "total": 0,
        "has_more": False,
    }
