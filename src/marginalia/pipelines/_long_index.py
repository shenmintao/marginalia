"""Helpers for description-first long-document indexing.

Long files are indexed in two layers:

1. Chunk calls produce `description.sections` with stable anchors.
2. An aggregate call reads those section summaries and produces file-level
   summary/tags/extra/catalog fields.

This module keeps the plumbing shared between PDF and text pipelines while
leaving file-type extraction and readback logic in the concrete pipelines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marginalia.llm.tagged_response import (
    parse_path,
    parse_sections,
    parse_tagged,
    parse_tags,
)
from marginalia.pipelines.base import TagSuggestion


@dataclass(slots=True)
class IndexFields:
    summary: str
    description_text: str | None
    sections: list[dict[str, Any]]
    extra: str | None
    entry_extra: str | None
    catalog_path: list[str] | None
    tags: list[TagSuggestion]


def parse_index_response(resp: Any, *, anchor_unit: str) -> IndexFields:
    """Parse either the current tagged format or older parsed_json fakes.

    Some local e2e tests still return `parsed_json`; production prompts ask
    for tagged text. Supporting both here keeps tests cheap and avoids
    duplicating parser fallback code in every pipeline.
    """
    payload = getattr(resp, "parsed_json", None)
    if isinstance(payload, dict):
        return _parse_json_payload(payload, anchor_unit=anchor_unit)

    tagged = parse_tagged(getattr(resp, "text", None) or "")
    return IndexFields(
        summary=tagged.get("summary", "").strip(),
        description_text=_clean(tagged.get("description")),
        sections=parse_sections(
            tagged.get("sections", ""), anchor_unit=anchor_unit,
        ),
        extra=_clean(tagged.get("extra")),
        entry_extra=_clean(tagged.get("entry_extra")),
        catalog_path=parse_path(tagged.get("catalog_path", "")) or None,
        tags=[
            TagSuggestion(name=t["name"], facet=t["facet"])
            for t in parse_tags(tagged.get("tags", ""))
        ],
    )


def renumber_sections(
    sections: list[dict[str, Any]], *, start: int = 1,
) -> list[dict[str, Any]]:
    """Return a copied section list with dense `sN` ids."""
    out: list[dict[str, Any]] = []
    for offset, sec in enumerate(sections):
        item = dict(sec)
        item["id"] = f"s{start + offset}"
        out.append(item)
    return out


def fallback_section(
    *,
    title: str,
    anchor_unit: str,
    anchor_value: str,
    summary: str,
    key_terms: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": "",
        "title": title,
        "anchor": {"unit": anchor_unit, "value": anchor_value},
        "summary": summary,
        "key_terms": list(key_terms or []),
    }


def build_retrieval_extra(
    *,
    sections: list[dict[str, Any]],
    coverage: dict[str, Any],
    base_extra: str | None = None,
    max_terms: int = 160,
    max_sections: int = 80,
) -> str:
    """Build a compact, searchable file-level `extra` digest.

    `description.sections` remains the full map. This digest is just the
    file-level recall surface searched by `search_metadata(text=...)`.
    """
    lines: list[str] = []
    if base_extra and base_extra.strip():
        lines.append(base_extra.strip())

    unit = str(coverage.get("unit") or "items")
    total = coverage.get("total_pages", coverage.get("total_units", "?"))
    indexed = coverage.get("indexed_pages", coverage.get("indexed_units", "?"))
    chunk_count = coverage.get("chunk_count", 1)
    partial = bool(coverage.get("indexed_partial"))
    lines.append(
        f"indexed_coverage: {indexed}/{total} {unit}; "
        f"partial={str(partial).lower()}; chunk_count={chunk_count}"
    )

    terms = _section_terms(sections)
    if terms:
        lines.append("retrieval_terms: " + "; ".join(terms[:max_terms]))

    bits: list[str] = []
    for sec in sections[:max_sections]:
        sid = str(sec.get("id") or "")
        title = str(sec.get("title") or "").strip()
        anchor = sec.get("anchor") or {}
        if isinstance(anchor, dict):
            anchor_value = str(anchor.get("value") or anchor.get("path") or "")
        else:
            anchor_value = str(anchor)
        summary = str(sec.get("summary") or "").strip()
        bit = " ".join(p for p in (sid, anchor_value, title, summary) if p)
        if bit:
            bits.append(bit[:220])
    if bits:
        suffix = ""
        if len(sections) > max_sections:
            suffix = f" | ... {len(sections) - max_sections} more sections"
        lines.append("section_map: " + " | ".join(bits) + suffix)

    return "\n".join(lines)


def render_sections_digest(
    sections: list[dict[str, Any]], *, max_chars: int = 60_000,
) -> str:
    """Render sections as compact lines for the aggregate LLM call."""
    lines: list[str] = []
    total = 0
    for sec in sections:
        anchor = sec.get("anchor") or {}
        if isinstance(anchor, dict):
            anchor_value = str(anchor.get("value") or anchor.get("path") or "")
        else:
            anchor_value = str(anchor)
        terms = sec.get("key_terms") or []
        if isinstance(terms, list):
            terms_text = ", ".join(str(t) for t in terms if str(t).strip())
        else:
            terms_text = str(terms)
        line = (
            f"{sec.get('id', '')} | {anchor_value} | "
            f"{sec.get('title', '')} | {sec.get('summary', '')} | {terms_text}"
        )
        if total + len(line) + 1 > max_chars:
            lines.append(f"... truncated section digest at {len(lines)} sections")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _parse_json_payload(payload: dict[str, Any], *, anchor_unit: str) -> IndexFields:
    desc = payload.get("description")
    description_text: str | None = None
    sections: list[dict[str, Any]] = []
    if isinstance(desc, dict):
        description_text = _clean(desc.get("text") or desc.get("description"))
        sections = _coerce_sections(desc.get("sections"), anchor_unit=anchor_unit)
    elif isinstance(desc, str):
        description_text = _clean(desc)

    tags: list[TagSuggestion] = []
    raw_entry_tags = payload.get("entry_tags")
    if isinstance(raw_entry_tags, list):
        for item in raw_entry_tags:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            facet = str(item.get("facet") or "").strip()
            if name and facet:
                tags.append(TagSuggestion(name=name, facet=facet))
    if not tags:
        tags = [
            TagSuggestion(name=t["name"], facet=t["facet"])
            for t in parse_tags(str(payload.get("tags") or ""))
        ]

    raw_path = payload.get("entry_catalog_path") or payload.get("catalog_path")
    if isinstance(raw_path, list):
        catalog_path = [str(p).strip() for p in raw_path if str(p).strip()]
    else:
        catalog_path = parse_path(str(raw_path or ""))

    return IndexFields(
        summary=str(payload.get("summary") or "").strip(),
        description_text=description_text,
        sections=sections,
        extra=_clean(payload.get("extra")),
        entry_extra=_clean(payload.get("entry_extra")),
        catalog_path=catalog_path or None,
        tags=tags,
    )


def _coerce_sections(raw: Any, *, anchor_unit: str) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for idx, sec in enumerate(raw, start=1):
        if not isinstance(sec, dict):
            continue
        item = dict(sec)
        item["id"] = str(item.get("id") or f"s{idx}")
        anchor = item.get("anchor")
        if isinstance(anchor, dict):
            value = anchor.get("value", anchor.get("path", ""))
            item["anchor"] = {
                "unit": str(anchor.get("unit") or anchor_unit),
                "value": str(value),
            }
        else:
            item["anchor"] = {"unit": anchor_unit, "value": str(anchor or "")}
        terms = item.get("key_terms") or []
        if isinstance(terms, str):
            item["key_terms"] = [
                t.strip() for t in terms.split(",") if t.strip()
            ]
        elif isinstance(terms, list):
            item["key_terms"] = [str(t).strip() for t in terms if str(t).strip()]
        else:
            item["key_terms"] = []
        item["title"] = str(item.get("title") or "").strip()
        item["summary"] = str(item.get("summary") or "").strip()
        out.append(item)
    return out


def _section_terms(sections: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sec in sections:
        raw_terms = sec.get("key_terms") or []
        if isinstance(raw_terms, str):
            terms = [t.strip() for t in raw_terms.split(",")]
        elif isinstance(raw_terms, list):
            terms = [str(t).strip() for t in raw_terms]
        else:
            terms = []
        terms.append(str(sec.get("title") or "").strip())
        for term in terms:
            if not term:
                continue
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(term)
    return out


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
