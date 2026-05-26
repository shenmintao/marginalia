"""Tagged response parser — replaces JSON-schema structured output.

Why this exists: reasoning models (qwen3.6, o-series, deepseek-r1) blow
their token budget on reasoning when forced to produce nested JSON,
ending with `content=''`. This module asks for `<tag>...</tag>` blocks
with shallow line-oriented payloads instead — no escaping, no nesting,
no fences. The model writes prose where prose belongs and short tabular
data only for `sections` / `tags`.

Format:

    <summary>
    free-form prose
    </summary>

    <description>
    free-form prose, may be multi-paragraph
    </description>

    <sections>
    s1 | pages 1-3 | Introduction | one-or-two-sentence summary | term1, term2
    s2 | pages 4-7 | Methods      | ...                          | term3
    </sections>

    <extra>prose; may be empty</extra>
    <entry_extra>prose; may be empty</entry_extra>
    <catalog_path>Research / LLM</catalog_path>
    <tags>
    topic: llm, reasoning
    form: paper
    language: zh
    </tags>

Parsers tolerate missing tags (return "" / [] for absent ones), trim
whitespace, and accept ``` ``` fences around individual blocks.
"""
from __future__ import annotations

import re
from typing import Any

# Allow fences and stray whitespace inside the tag body.
_TAG_RE = re.compile(
    r"<\s*(?P<name>[a-z_][a-z0-9_]*)\s*>(?P<body>.*?)<\s*/\s*(?P=name)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n|\n```$", re.MULTILINE)


def parse_tagged(text: str) -> dict[str, str]:
    """Pull every `<tag>...</tag>` block out of `text`.

    Repeated tags: the LAST occurrence wins. Reasoning models often
    "draft then refine" — the second pass is what we want.
    """
    out: dict[str, str] = {}
    for m in _TAG_RE.finditer(text or ""):
        body = m.group("body").strip()
        body = _FENCE_RE.sub("", body).strip()
        out[m.group("name").lower()] = body
    return out


# ---- field-specific helpers -----------------------------------------------

_VALID_FACETS = ("topic", "form", "time", "source", "language", "extra")


def parse_kv(block: str) -> dict[str, str]:
    """Parse `key: value` lines into a dict.

    LLMs are MUCH happier writing this than nested JSON — no escaping,
    no quotes, no comma bookkeeping. Each line `key: value` becomes one
    entry. Lines without `:`, comment lines (`#`), and blank lines are
    skipped. Values are stripped strings — this parser does NOT cast to
    int/bool/list. The consumer decides how to interpret each field
    (e.g. `int(extra["ocr_pages"])`).

    Repeated keys: last write wins.
    """
    out: dict[str, str] = {}
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower().replace(" ", "_")
        if not k:
            continue
        out[k] = v.strip()
    return out


def parse_path(block: str) -> list[str]:
    """`Research / LLM / Reasoning` → ['Research', 'LLM', 'Reasoning'].

    Empty / missing block → []. Tolerates `>` and `→` separators too —
    reasoning models reach for them.
    """
    if not block.strip():
        return []
    parts = re.split(r"\s*[/>→]\s*", block.strip())
    return [p for p in (s.strip() for s in parts) if p]


def parse_tags(block: str) -> list[dict[str, str]]:
    """Parse the `<tags>` block.

    Supports two shapes the model might pick:

      facet: name1, name2, name3
      facet: name4

      OR, line-per-tag:
      topic: llm
      topic: reasoning
      form: paper

    Lines without a recognised facet prefix are dropped silently — the
    handler will warn if it gets zero tags. Names are lowercased and
    de-duplicated within a facet.
    """
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in (block or "").splitlines():
        line = raw.strip().lstrip("-*•").strip()
        if not line or ":" not in line:
            continue
        facet, names = line.split(":", 1)
        facet = facet.strip().lower()
        if facet not in _VALID_FACETS:
            continue
        for name in names.split(","):
            name = name.strip().strip('"').strip("'")
            if not name:
                continue
            key = (facet, name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"name": name, "facet": facet})
    return out


def parse_sections(
    block: str, *, anchor_unit: str = "heading",
) -> list[dict[str, Any]]:
    """Parse the `<sections>` block.

    Each non-empty line:

        s1 | <anchor-value> | <title> | <summary> | <term1, term2, ...>

    `anchor_unit` is supplied by the caller (pipeline knows whether it
    deals in pages, headings, or lines). The parser keeps it dumb:
    splits on `|`, trims, requires at least 4 fields. Lines with fewer
    fields are skipped silently. id is auto-assigned (s1, s2, ...) if
    the line skips it.
    """
    out: list[dict[str, Any]] = []
    auto_idx = 0
    for raw in (block or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            continue
        # Detect whether the first field is an id (sNN / N) or anchor.
        head = parts[0]
        if re.fullmatch(r"s?\d+", head, re.IGNORECASE):
            sid = head if head.lower().startswith("s") else f"s{head}"
            anchor_value, title, summary, *terms_parts = parts[1:]
        else:
            auto_idx += 1
            sid = f"s{auto_idx}"
            anchor_value, title, summary, *terms_parts = parts
        if not auto_idx and sid.lower().startswith("s"):
            try:
                auto_idx = max(auto_idx, int(sid[1:]))
            except ValueError:
                pass
        terms_raw = " | ".join(terms_parts) if terms_parts else ""
        key_terms = [
            t.strip() for t in terms_raw.split(",") if t.strip()
        ]
        out.append({
            "id": sid,
            "title": title,
            "anchor": {"unit": anchor_unit, "value": anchor_value},
            "summary": summary,
            "key_terms": key_terms,
        })
    return out


def render_format_hint(*, kinds: tuple[str, ...] | None = None) -> str:
    """Inject this into a system prompt so the model knows the format.

    Kept short — the tag names ARE the docs. `kinds` lets the caller
    constrain the `<kind>` block (e.g. "image", "text", "container").
    """
    kind_hint = ""
    if kinds:
        kind_hint = (
            "\n  <kind>one of: " + " | ".join(kinds) + "</kind>"
        )
    return (
        "Output format — use these tags, in any order, in plain text:\n"
        "  <summary>2-4 sentences of prose.</summary>\n"
        "  <description>free-form prose; multi-paragraph OK.</description>"
        + kind_hint + "\n"
        "  <extra>\n"
        "  key: value\n"
        "  another_key: another value\n"
        "  </extra>  (machine-readable insights; one key:value per line; "
        "OK to leave empty)\n"
        "  <entry_extra>same key:value shape; OK empty</entry_extra>\n"
        "  <catalog_path>Top / Sub / Leaf</catalog_path>\n"
        "  <tags>\n"
        "  topic: name1, name2\n"
        "  form: name3\n"
        "  language: zh\n"
        "  </tags>\n"
        "Do NOT wrap in JSON. Do NOT add ``` fences around the whole "
        "response. Tag bodies are plain text."
    )


def render_sections_hint(anchor_unit: str, anchor_example: str) -> str:
    """Sections block hint, separate so pipelines that don't need
    sections (image, archive) can skip it."""
    return (
        "  <sections>\n"
        f"  s1 | {anchor_example} | Section title | "
        "1-2 sentence summary | term1, term2, term3\n"
        "  s2 | ... | ... | ... | ...\n"
        f"  </sections>  (anchor unit: {anchor_unit})"
    )
