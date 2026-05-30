"""recall_knowledge - deterministic first-pass knowledge-base recall.

This is a thin orchestration layer over the existing recall tools. It keeps
the fixed "resolve tags, search journal, search metadata" path in code so the
agent prompt does not need to carry that workflow in detail.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.text_query import normalize_text_queries
from marginalia.agent.tools import ToolContext, tool
from marginalia.agent.tools.resolve_tag import resolve_tag
from marginalia.agent.tools.search_journal import run_search_journal
from marginalia.agent.tools.search_metadata import search_metadata
from marginalia.config import get_settings
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_relations as relations_repo
from marginalia.semantic.index import semantic_entry_rows, semantic_recall_configured
from marginalia.semantic.rerank import RerankHit, get_rerank_client, rerank_configured


DEFAULT_LIMIT = 100
MAX_LIMIT = 100
VERIFY_BATCH_LIMIT = 50
NOTE_PREVIEW_CHARS = 300
SUMMARY_PREVIEW_CHARS = 300
TAG_FACETS = {"topic", "form", "time", "source", "language", "extra"}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [],
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Candidate tag names from the plan. Names are resolved before "
                "metadata tag search; unresolved names become text fallback."
            ),
        },
        "text": {
            "anyOf": [
                {"type": "string"},
                {"type": "array", "items": {"type": "string"}},
            ],
            "description": (
                "Candidate keywords or short phrases. Array items are ORed."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": "Max candidates returned. Default 100.",
        },
    },
}


@tool(
    name="recall_knowledge",
    description=(
        "Preferred high-level entrypoint for broad knowledge-base material "
        "recall. Resolves tag hints, searches journal notes and entry "
        "metadata, then returns compact candidate entries for verification "
        "and file reading."
    ),
    schema=SCHEMA,
)
async def recall_knowledge(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    raw_tags = _dedupe(_string_list(args.get("tags")))
    text_terms = _dedupe(normalize_text_queries(args.get("text")))
    limit = _limit(args.get("limit"))
    fetch_limit = MAX_LIMIT

    resolved_tags: list[dict[str, Any]] = []
    unresolved_terms: list[str] = []
    metadata_tag_ids: list[str] = []
    journal_tag_terms: list[str] = []

    for tag in raw_tags:
        tag_name, facet = _parse_tag_seed(tag)
        resolve_args: dict[str, Any] = {"name": tag_name}
        if facet is not None:
            resolve_args["facet"] = facet
        result = await resolve_tag(db, ctx, resolve_args)
        if result.get("found"):
            resolved = {
                "input": tag,
                "id": result.get("id"),
                "name": result.get("name"),
                "facet": result.get("facet"),
                "via": result.get("via"),
                "was_alias": bool(result.get("was_alias")),
            }
            resolved_tags.append(resolved)
            _append_unique(metadata_tag_ids, str(result["id"]))
            for term in _journal_tag_variants(tag, result):
                _append_unique(journal_tag_terms, term)
        else:
            unresolved_terms.append(tag)
            _append_unique(journal_tag_terms, tag)
            _append_unique(text_terms, tag_name)

    note_map: dict[str, dict[str, Any]] = {}
    trace: dict[str, Any] = {}

    if journal_tag_terms or text_terms:
        result = await run_search_journal(
            db,
            {"tags": journal_tag_terms, "text": text_terms, "limit": fetch_limit},
            match="any",
        )
        trace["journal"] = int(result.get("count") or 0)
        _merge_notes(note_map, result.get("notes") or [], "journal")

    entry_map: dict[str, dict[str, Any]] = {}

    if metadata_tag_ids:
        result = await search_metadata(
            db, ctx, {"tags_any": metadata_tag_ids, "limit": fetch_limit},
        )
        trace["metadata_tags"] = int(result.get("count") or 0)
        _merge_entries(entry_map, result.get("entries") or [], "metadata_tags")

    if text_terms:
        result = await search_metadata(
            db, ctx, {"text": text_terms, "limit": fetch_limit},
        )
        trace["metadata_text"] = int(result.get("count") or 0)
        _merge_entries(entry_map, result.get("entries") or [], "metadata_text")

    settings = get_settings()
    if text_terms and semantic_recall_configured():
        try:
            semantic_rows = await semantic_entry_rows(
                db,
                " ".join(text_terms),
                limit=min(fetch_limit, settings.semantic_recall_limit),
            )
        except Exception as exc:  # noqa: BLE001
            trace["semantic_error"] = 1
            trace["semantic_error_message"] = str(exc)[:200]
        else:
            trace["semantic"] = len(semantic_rows)
            _merge_entries(entry_map, semantic_rows, "semantic")

    ranked_entries = score_recall_entries(
        list(entry_map.values()),
        text_terms=text_terms,
    )
    rerank_trace: dict[str, Any] = {"enabled": False}
    if text_terms and rerank_configured(settings):
        ranked_entries, rerank_trace = await rerank_recall_entries(
            db,
            ranked_entries,
            query=" ".join(text_terms),
        )
    entries = select_evidence_entries(ranked_entries, limit)
    notes = list(note_map.values())[:limit]
    candidate_entry_ids = _candidate_entry_ids(notes, entries, limit)
    expansion_entry_ids = await _one_hop_expansion_ids(
        db, candidate_entry_ids, limit=limit,
    )
    verify_entry_ids = _verification_batch(candidate_entry_ids, expansion_entry_ids)

    return {
        "resolved_tags": resolved_tags,
        "unresolved_terms": unresolved_terms,
        "text_terms": text_terms,
        "notes": notes,
        "entries": entries,
        "candidate_entry_ids": candidate_entry_ids,
        "expansion_entry_ids": expansion_entry_ids,
        "verify_entry_ids": verify_entry_ids,
        "count": {
            "notes": len(notes),
            "entries": len(entries),
            "candidate_entry_ids": len(candidate_entry_ids),
            "expansion_entry_ids": len(expansion_entry_ids),
            "verify_entry_ids": len(verify_entry_ids),
        },
        "limit": limit,
        "fetch_limit": fetch_limit,
        "trace": {
            **trace,
            "scoring": {
                "ranker": "rrf_heuristic_v1",
                "entries_ranked": len(ranked_entries),
                "evidence_selection": settings.evidence_selection,
                "evidence_quota": (
                    _evidence_quota_trace(limit)
                    if settings.evidence_selection == "quota"
                    else None
                ),
            },
            "rerank": rerank_trace,
        },
    }


def _limit(value: Any) -> int:
    try:
        n = int(value or DEFAULT_LIMIT)
    except (TypeError, ValueError):
        n = DEFAULT_LIMIT
    return max(1, min(n, MAX_LIMIT))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _parse_tag_seed(value: str) -> tuple[str, str | None]:
    facet, sep, name = value.partition(":")
    if sep and facet in TAG_FACETS and name.strip():
        return name.strip(), facet
    return value, None


def _append_unique(items: list[str], item: str) -> None:
    if item and item.casefold() not in {existing.casefold() for existing in items}:
        items.append(item)


def _journal_tag_variants(input_name: str, resolved: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for item in (input_name, resolved.get("name")):
        if isinstance(item, str) and item.strip():
            _append_unique(out, item.strip())
    facet = resolved.get("facet")
    name = resolved.get("name")
    if isinstance(facet, str) and isinstance(name, str) and name.strip():
        _append_unique(out, f"{facet}:{name.strip()}")
    return out


def _merge_notes(
    note_map: dict[str, dict[str, Any]],
    notes: list[Any],
    source: str,
) -> None:
    for note in notes:
        if not isinstance(note, dict):
            continue
        note_id = str(note.get("id") or "")
        if not note_id:
            continue
        existing = note_map.get(note_id)
        if existing is None:
            existing = {
                "id": note_id,
                "note": _truncate(str(note.get("note") or ""), NOTE_PREVIEW_CHARS),
                "entry_ids": list(note.get("entry_ids") or []),
                "tags": list(note.get("tags") or []),
                "source_kind": note.get("source_kind"),
                "created_at": note.get("created_at"),
                "matched_by": [],
            }
            note_map[note_id] = existing
        _append_unique(existing["matched_by"], source)


def _merge_entries(
    entry_map: dict[str, dict[str, Any]],
    entries: list[Any],
    source: str,
) -> None:
    total = len(entries)
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        entry_id = str(entry.get("entry_id") or "")
        if not entry_id:
            continue
        existing = entry_map.get(entry_id)
        if existing is None:
            existing = {
                "entry_id": entry_id,
                "display_name": entry.get("display_name"),
                "lifecycle": entry.get("lifecycle"),
                "kind": entry.get("kind"),
                "summary": _truncate(
                    str(entry.get("summary") or ""), SUMMARY_PREVIEW_CHARS,
                ),
                "catalog_id": entry.get("catalog_id"),
                "folder_id": entry.get("folder_id"),
                "coverage": entry.get("coverage"),
                "matched_by": [],
                "score": 0.0,
                "rank_score": 0,
                "rrf_score": 0.0,
                "score_components": {},
            }
            entry_map[entry_id] = existing
        _append_unique(existing["matched_by"], source)
        rank_key = _rank_key_for_source(source)
        if rank_key:
            rank = idx + 1
            existing[rank_key] = min(int(existing.get(rank_key) or rank), rank)
        existing["rank_score"] = max(
            int(existing.get("rank_score") or 0),
            total - idx,
        )
        existing["rrf_score"] = _rrf_score(existing)


def score_recall_entries(
    entries: list[dict[str, Any]],
    *,
    text_terms: list[str],
) -> list[dict[str, Any]]:
    query_terms = _score_terms(text_terms)
    for row in entries:
        components = _score_components(row, query_terms)
        row["score_components"] = {
            key: round(value, 4)
            for key, value in components.items()
            if abs(value) > 0.0001
        }
        row["score"] = round(sum(components.values()), 4)
    return sorted(entries, key=_entry_sort_key)


def select_quota_entries(
    ranked: list[dict[str, Any]],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    overlap_quota, tag_quota, lexical_quota, semantic_quota = _evidence_quotas(limit)
    groups: dict[str, list[dict[str, Any]]] = {
        "overlap": [],
        "tag": [],
        "lexical": [],
        "semantic": [],
    }
    for row in ranked:
        bucket = _entry_bucket(row)
        if bucket in groups:
            groups[bucket].append(row)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(rows: list[dict[str, Any]], quota: int) -> None:
        for row in rows:
            if len(out) >= limit or quota <= 0:
                return
            entry_id = str(row.get("entry_id") or "")
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            out.append(row)
            quota -= 1

    take(groups["overlap"], overlap_quota)
    take(groups["tag"], tag_quota)
    take(groups["lexical"], lexical_quota)
    take(groups["semantic"], semantic_quota)
    take(ranked, limit - len(out))
    return out[:limit]


def select_quota_entry_ids(
    ranked: list[dict[str, Any]],
    limit: int,
) -> list[str]:
    return [
        str(row["entry_id"])
        for row in select_quota_entries(ranked, limit)
        if row.get("entry_id")
    ]


def select_evidence_entries(
    ranked: list[dict[str, Any]],
    limit: int,
    *,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    strategy = strategy or get_settings().evidence_selection
    if strategy == "rerank":
        return ranked[:max(0, limit)]
    return select_quota_entries(ranked, limit)


def select_evidence_entry_ids(
    ranked: list[dict[str, Any]],
    limit: int,
    *,
    strategy: str | None = None,
) -> list[str]:
    return [
        str(row["entry_id"])
        for row in select_evidence_entries(ranked, limit, strategy=strategy)
        if row.get("entry_id")
    ]


async def rerank_recall_entries(
    db: AsyncSession,
    ranked: list[dict[str, Any]],
    *,
    query: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    top_n = max(1, min(len(ranked), int(settings.rerank_top_n or 80)))
    if not ranked or top_n <= 0:
        return ranked, {"enabled": bool(settings.rerank_enabled), "count": 0}
    top = ranked[:top_n]
    documents_by_id = await load_rerank_documents_by_entry_id(
        db,
        [str(row.get("entry_id") or "") for row in top],
    )
    return await rerank_recall_entries_with_documents(
        ranked,
        query=query,
        documents_by_id=documents_by_id,
    )


async def rerank_recall_entries_with_documents(
    ranked: list[dict[str, Any]],
    *,
    query: str,
    documents_by_id: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    settings = get_settings()
    top_n = max(1, min(len(ranked), int(settings.rerank_top_n or 80)))
    if not ranked or top_n <= 0:
        return ranked, {"enabled": bool(settings.rerank_enabled), "count": 0}
    top = ranked[:top_n]
    documents = [
        documents_by_id.get(str(row.get("entry_id") or ""), _fallback_rerank_text(row))
        for row in top
    ]
    try:
        hits = await get_rerank_client(settings).rerank(
            query,
            documents,
            top_n=len(documents),
        )
    except Exception as exc:  # noqa: BLE001
        return ranked, {
            "enabled": True,
            "attempted": top_n,
            "error": str(exc)[:200],
        }
    if not hits:
        return ranked, {"enabled": True, "attempted": top_n, "count": 0}
    return apply_rerank_hits(ranked, hits, top_n=top_n), {
        "enabled": True,
        "attempted": top_n,
        "count": len(hits),
        "model": settings.rerank_model,
    }


def apply_rerank_hits(
    ranked: list[dict[str, Any]],
    hits: list[RerankHit],
    *,
    top_n: int,
) -> list[dict[str, Any]]:
    top_n = max(0, min(top_n, len(ranked)))
    if top_n <= 0:
        return ranked
    score_by_index = {
        hit.index: hit.score
        for hit in hits
        if 0 <= hit.index < top_n
    }
    rank_by_index = {
        hit.index: hit.rank
        for hit in hits
        if 0 <= hit.index < top_n
    }
    if not score_by_index:
        return ranked

    top = ranked[:top_n]
    for idx, row in enumerate(top):
        if idx in score_by_index:
            row["rerank_score"] = round(score_by_index[idx], 6)
            row["rerank_rank"] = rank_by_index[idx]
            components = row.get("score_components")
            if isinstance(components, dict):
                components["rerank"] = round(score_by_index[idx], 6)

    reranked_top = sorted(
        enumerate(top),
        key=lambda item: (
            -float(score_by_index.get(item[0], float("-inf"))),
            item[0],
        ),
    )
    return [row for _idx, row in reranked_top] + ranked[top_n:]


async def load_rerank_documents_by_entry_id(
    db: AsyncSession,
    entry_ids: list[str],
) -> dict[str, str]:
    clean = [entry_id for entry_id in _dedupe(entry_ids) if entry_id]
    if not clean:
        return {}
    settings = get_settings()
    rows = await entries_repo.list_live_with_file_by_ids(db, clean)
    by_id = {entry.id: (entry, file_row) for entry, file_row in rows}
    out: dict[str, str] = {}
    for entry_id in clean:
        pair = by_id.get(entry_id)
        if pair is None:
            continue
        entry, file_row = pair
        out[entry_id] = _truncate(
            _rerank_document_text(entry, file_row),
            max(200, int(settings.rerank_max_doc_chars or 1800)),
        )
    return out


def _rerank_document_text(entry: Any, file_row: Any) -> str:
    parts = [
        f"title: {getattr(entry, 'display_name', '') or ''}",
        f"summary: {getattr(file_row, 'summary', '') or ''}",
        _description_text(getattr(file_row, "description", None)),
        f"file_extra: {getattr(file_row, 'extra', '') or ''}",
        f"entry_extra: {getattr(entry, 'extra', '') or ''}",
    ]
    return "\n".join(part for part in parts if part.strip())


def _description_text(description: Any) -> str:
    if isinstance(description, str):
        return description
    if not isinstance(description, Mapping):
        return ""
    parts: list[str] = []
    text = description.get("text")
    if isinstance(text, str):
        parts.append(text)
    sections = description.get("sections")
    if isinstance(sections, list):
        for section in sections[:8]:
            if not isinstance(section, Mapping):
                continue
            title = section.get("title")
            summary = section.get("summary")
            key_terms = section.get("key_terms")
            line = " ".join(
                _stringify(item)
                for item in (title, summary, key_terms)
                if item
            )
            if line:
                parts.append(line)
    return "\n".join(parts)


def _fallback_rerank_text(row: Mapping[str, Any]) -> str:
    return "\n".join(
        str(item)
        for item in (
            f"title: {row.get('display_name') or ''}",
            f"summary: {row.get('summary') or ''}",
            f"kind: {row.get('kind') or ''}",
        )
        if item
    )


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(item) for item in value)
    if isinstance(value, Mapping):
        return " ".join(_stringify(item) for item in value.values())
    if value is None:
        return ""
    return str(value)


def _score_components(
    row: Mapping[str, Any],
    query_terms: list[str],
) -> dict[str, float]:
    rrf = _rrf_score(row) * 1000.0
    components = {
        "rrf": rrf,
        "source_overlap": _source_overlap_score(row),
        "field_match": _field_match_score(row, query_terms),
        "evidence_utility": _evidence_utility_score(row),
    }
    return components


def _rank_key_for_source(source: str) -> str | None:
    if source == "metadata_tags":
        return "tag_rank"
    if source == "metadata_text":
        return "lexical_rank"
    if source == "semantic":
        return "semantic_rank"
    return None


def _rrf_score(row: Mapping[str, Any], *, k: int = 60) -> float:
    score = 0.0
    for key in ("tag_rank", "lexical_rank", "semantic_rank"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            rank = int(raw)
        except (TypeError, ValueError):
            continue
        if rank > 0:
            score += 1.0 / (k + rank)
    return score


def _source_overlap_score(row: Mapping[str, Any]) -> float:
    matched_by = set(row.get("matched_by") or [])
    score = 0.0
    if "metadata_tags" in matched_by:
        score += 1.2
    if "metadata_text" in matched_by and "semantic" in matched_by:
        score += 1.5
    if "metadata_tags" in matched_by and "metadata_text" in matched_by:
        score += 0.8
    if "metadata_tags" in matched_by and "semantic" in matched_by:
        score += 0.5
    return score


def _field_match_score(row: Mapping[str, Any], query_terms: list[str]) -> float:
    if not query_terms:
        return 0.0
    fields = [
        (row.get("display_name"), 2.4),
        (row.get("summary"), 1.4),
        (row.get("kind"), 0.2),
    ]
    raw_score = 0.0
    covered: set[str] = set()
    for term in query_terms:
        term_score = 0.0
        for raw, weight in fields:
            hits = _term_hits(raw, term)
            if hits:
                term_score += weight * min(hits, 2)
        if term_score:
            covered.add(term.casefold())
            raw_score += term_score * _term_weight(term)
    coverage_score = 1.0 * (len(covered) / len(query_terms))
    return min(raw_score, 2.0) + coverage_score


def _evidence_utility_score(row: Mapping[str, Any]) -> float:
    score = 0.0
    if str(row.get("summary") or "").strip():
        score += 0.4
    coverage = row.get("coverage")
    if isinstance(coverage, Mapping):
        if coverage.get("indexed_partial") is False:
            score += 0.3
        if coverage.get("chunk_count") or coverage.get("indexed_pages"):
            score += 0.2
        if coverage.get("text_truncated") or coverage.get("sampled"):
            score -= 0.2
    return score


_SCORE_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_./-]*")
_SCORE_STOPWORDS = {
    "about",
    "after",
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


def _score_terms(text_terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in text_terms:
        text = str(raw or "").strip()
        if not text:
            continue
        candidates = [text]
        candidates.extend(_SCORE_TOKEN_RE.findall(text))
        for candidate in candidates:
            term = candidate.strip(".,;:!?()[]{}\"'")
            key = term.casefold()
            if not term or key in seen or key in _SCORE_STOPWORDS:
                continue
            has_digit = any(ch.isdigit() for ch in term)
            has_upper = any(ch.isupper() for ch in term)
            if len(term) < 4 and not has_digit and not has_upper:
                continue
            seen.add(key)
            out.append(term)
    return out


def _term_hits(raw: Any, term: str) -> int:
    if raw is None:
        return 0
    needle = term.casefold()
    if not needle:
        return 0
    return str(raw).casefold().count(needle)


def _term_weight(term: str) -> float:
    weight = 1.0
    if len(term) >= 7:
        weight += 0.35
    if any(ch.isdigit() for ch in term):
        weight += 0.6
    if any(ch.isupper() for ch in term):
        weight += 0.4
    if any(ch in term for ch in "/+-_."):
        weight += 0.3
    return weight


def _entry_bucket(row: Mapping[str, Any]) -> str:
    matched_by = set(row.get("matched_by") or [])
    has_lexical = "metadata_text" in matched_by
    has_semantic = "semantic" in matched_by
    if has_lexical and has_semantic:
        return "overlap"
    if "metadata_tags" in matched_by:
        return "tag"
    if has_lexical:
        return "lexical"
    if has_semantic:
        return "semantic"
    return "other"


def _evidence_quotas(limit: int) -> tuple[int, int, int, int]:
    if limit <= 1:
        return limit, 0, 0, 0
    overlap = max(1, round(limit * 0.35))
    tag = max(1, round(limit * 0.2))
    lexical = max(1, round(limit * 0.25))
    semantic = max(0, limit - overlap - tag - lexical)
    return overlap, tag, lexical, semantic


def _evidence_quota_trace(limit: int) -> dict[str, int]:
    overlap, tag, lexical, semantic = _evidence_quotas(limit)
    return {
        "overlap": overlap,
        "tag": tag,
        "lexical": lexical,
        "semantic": semantic,
    }


def _entry_sort_key(row: Mapping[str, Any]) -> tuple[float, float, int, str]:
    return (
        -float(row.get("score") or 0.0),
        -float(row.get("rrf_score") or 0.0),
        -int(row.get("rank_score") or 0),
        str(row.get("display_name") or ""),
    )


def _candidate_entry_ids(
    notes: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    limit: int,
) -> list[str]:
    out: list[str] = []
    for note in notes:
        for entry_id in note.get("entry_ids") or []:
            _append_unique(out, str(entry_id))
            if len(out) >= limit:
                return out
    for entry in entries:
        entry_id = entry.get("entry_id")
        if entry_id:
            _append_unique(out, str(entry_id))
            if len(out) >= limit:
                return out
    return out


async def _one_hop_expansion_ids(
    db: AsyncSession,
    anchor_entry_ids: list[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    expansion: dict[str, dict[str, Any]] = {}
    anchors = set(anchor_entry_ids)
    if not anchor_entry_ids:
        return []
    per_anchor_limit = max(1, min(10, limit))
    for anchor_id in anchor_entry_ids[:limit]:
        rel_rows = await relations_repo.list_top_for_entry(
            db, anchor_id, limit=per_anchor_limit, vetted_only=True,
        )
        for relation in rel_rows:
            other_id = (
                relation.entry_b_id
                if relation.entry_a_id == anchor_id
                else relation.entry_a_id
            )
            if other_id in anchors:
                continue
            row = expansion.get(other_id)
            if row is None:
                row = {
                    "entry_id": other_id,
                    "matched_by": [],
                    "anchor_entry_ids": [],
                    "observation_count": relation.observation_count,
                }
                expansion[other_id] = row
            _append_unique(row["matched_by"], "vetted_relation")
            _append_unique(row["anchor_entry_ids"], anchor_id)
            row["observation_count"] = max(
                int(row.get("observation_count") or 0),
                int(relation.observation_count or 0),
            )
    return sorted(
        expansion.values(),
        key=lambda row: (-int(row.get("observation_count") or 0), row["entry_id"]),
    )[:limit]


def _verification_batch(
    candidate_entry_ids: list[str],
    expansion_entry_ids: list[dict[str, Any]],
) -> list[str]:
    out: list[str] = []
    for entry_id in candidate_entry_ids:
        _append_unique(out, entry_id)
        if len(out) >= VERIFY_BATCH_LIMIT:
            return out
    for row in expansion_entry_ids:
        entry_id = row.get("entry_id")
        if entry_id:
            _append_unique(out, str(entry_id))
            if len(out) >= VERIFY_BATCH_LIMIT:
                return out
    return out


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"
