"""recall_knowledge - deterministic first-pass knowledge-base recall.

This is a thin orchestration layer over the existing recall tools. It keeps
the fixed "resolve tags, search journal, search metadata" path in code so the
agent prompt does not need to carry that workflow in detail.
"""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.text_query import normalize_text_queries
from marginalia.agent.tools import ToolContext, tool
from marginalia.agent.tools.resolve_tag import resolve_tag
from marginalia.agent.tools.search_journal import run_search_journal
from marginalia.agent.tools.search_metadata import search_metadata
from marginalia.repositories import entry_relations as relations_repo


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
        "First-pass knowledge-base recall. Deterministically resolves plan "
        "tag seeds, searches journal notes, searches entry metadata, and "
        "returns compact candidates. Use before read_entries_metadata/read_files."
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
    trace: dict[str, int] = {}

    if journal_tag_terms or text_terms:
        result = await run_search_journal(
            db,
            {"tags": journal_tag_terms, "text": text_terms, "limit": limit},
            match="any",
        )
        trace["journal"] = int(result.get("count") or 0)
        _merge_notes(note_map, result.get("notes") or [], "journal")

    entry_map: dict[str, dict[str, Any]] = {}

    if metadata_tag_ids:
        result = await search_metadata(
            db, ctx, {"tags_any": metadata_tag_ids, "limit": limit},
        )
        trace["metadata_tags"] = int(result.get("count") or 0)
        _merge_entries(entry_map, result.get("entries") or [], "metadata_tags")

    if text_terms:
        result = await search_metadata(
            db, ctx, {"text": text_terms, "limit": limit},
        )
        trace["metadata_text"] = int(result.get("count") or 0)
        _merge_entries(entry_map, result.get("entries") or [], "metadata_text")

    notes = list(note_map.values())[:limit]
    entries = sorted(
        entry_map.values(),
        key=lambda row: (-int(row.get("score") or 0), row.get("display_name") or ""),
    )[:limit]
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
        "trace": trace,
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
    for entry in entries:
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
                "matched_by": [],
                "score": 0,
            }
            entry_map[entry_id] = existing
        _append_unique(existing["matched_by"], source)
        existing["score"] = _entry_score(existing["matched_by"])


def _entry_score(matched_by: list[str]) -> int:
    weights = {
        "metadata_tags": 3,
        "metadata_text": 2,
    }
    return sum(weights.get(source, 1) for source in matched_by)


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
