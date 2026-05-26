"""Shared LLM-indexing helper for text-shaped pipelines.

`text.py` was the first pipeline; docx / spreadsheet / log / code all
follow the same pattern: extract plain text, then ask the LLM to
produce the same structured index. This module pulls out the LLM call
so each parser only has to handle its own extraction.

Output uses the tagged-response format (see marginalia.llm.tagged_response).
Anchors stay heading-or-lines; sheet- and slide-aware variants can
introduce custom anchor units later if needed.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from marginalia.llm import ChatMessage, ChatRequest, TextBlock, get_chat_client
from marginalia.llm.tagged_response import (
    parse_path,
    parse_sections,
    parse_tagged,
    parse_tags,
    render_format_hint,
    render_sections_hint,
)
from marginalia.pipelines.base import PipelineContext, PipelineResult, TagSuggestion

log = logging.getLogger(__name__)

INDEXER_SYSTEM = """You are Marginalia's document indexer.

Your job: read a single document and produce a structured index that
lets a downstream agent decide whether to retrieve it, and once
retrieved, jump to the relevant section by anchor.

`summary` is one or two sentences (≤60 中文字 / ≤30 English words) in the
document's own language — the spine of what the document is and why a
reader would open it. Keep it tight; depth belongs in `description`.
`description` is a free-text walk-through of the document's organisation —
multi-paragraph if useful. `sections` lists every meaningful heading or
logical chunk; each line: `id | <heading-path or lines X-Y> | title |
one-or-two-sentence summary | term1, term2`. `extra` carries cross-cutting
machine-readable insights as `key: value` lines; leave empty if nothing
notable. `entry_extra` is the same shape but for position-aware insights.
`entry_catalog_path` is a best-guess classification path. `tags` are 3-10
facet:name pairs; valid facets are topic | form | time | source | language
| extra. Reuse names from the current vocabulary when they fit.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="heading or lines", anchor_example="1.2.3 or lines 100-160",
)


# Schema kept for legacy callers but no longer fed to the LLM.
def make_schema(kind: str) -> dict[str, Any]:
    return {}


async def index_extracted_text(
    body: str, ctx: PipelineContext, kind: str
) -> PipelineResult:
    """Run the indexing LLM call and return a PipelineResult."""
    user_payload = {
        "folder_path": ctx.folder_path,
        "sibling_names": ctx.sibling_names,
        "catalog_sketch": ctx.catalog_sketch,
        "tag_vocabulary": ctx.tag_vocabulary,
    }
    user_text = (
        f"Index the {kind} document below. Hints are advisory — the "
        "document's actual content takes precedence.\n\n"
        f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n"
        "</context>\n\n"
        f"<document>\n{body}\n</document>"
    )

    client = get_chat_client("ingest")
    # Budget covers reasoning + output. qwen3.6-plus and similar reasoning
    # models routinely burn 4-6k tokens on internal CoT before emitting
    # anything; a 2-3k cap leaves nothing for the actual <summary>/<sections>
    # block and the response comes back empty. Floor at 8k, scale up to 16k
    # for long bodies.
    max_out = min(16384, max(8192, len(body) // 8))

    resp = await client.complete(ChatRequest(
        system=INDEXER_SYSTEM,
        messages=[ChatMessage(
            role="user", content=[TextBlock(text=user_text)],
        )],
        max_tokens=max_out,
        temperature=0.2,
    ))

    tagged = parse_tagged(resp.text or "")
    summary = tagged.get("summary", "").strip()
    if not summary:
        log.warning(
            "%s pipeline: no <summary> in response. text=%r",
            kind, (resp.text or "")[:300],
        )
        raise ValueError(f"{kind} pipeline produced empty summary")

    sections = parse_sections(
        tagged.get("sections", ""), anchor_unit="heading",
    )
    description: dict[str, Any] = {"sections": sections}
    description_text = tagged.get("description", "").strip()
    if description_text:
        description["text"] = description_text

    return PipelineResult(
        summary=summary,
        description=description,
        kind=kind,
        extra=tagged.get("extra", "").strip() or None,
        entry_extra=tagged.get("entry_extra", "").strip() or None,
        entry_catalog_path=parse_path(tagged.get("catalog_path", "")) or None,
        entry_tags=[
            TagSuggestion(name=t["name"], facet=t["facet"])
            for t in parse_tags(tagged.get("tags", ""))
        ],
    )
