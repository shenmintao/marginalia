"""Conversation export — DESIGN.md agent identity (citation footnotes).

The agent finishes a turn with `agent_response` markdown that contains
`[^marker]` superscript references and footnotes of the form:

  [^a]: entry_id=019e...  (optionally: , section_id=s2)

This service parses those footnotes, resolves them to live entries, and
builds a zip plan: the report itself, each cited entry's file (deduped by
entry_id), each cited entry's user-facing metadata, and a manifest.

Boundary (DESIGN.md §14.3):
  - The exported `references/<name>.metadata.json` is the same shape as
    GET /file-entries/{id}/metadata — i.e. user-visible fields plus the
    librarian's summary, NEVER catalog_id / description / extra / tags.
  - References to soft-deleted or missing entries are recorded in the
    manifest under `missing` rather than aborting the export.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import Conversation
from marginalia.repositories import entries as entries_repo
from marginalia.services.user_files import get_user_metadata


_FOOTNOTE_RE = re.compile(
    r"\[\^([^\]]+)\]:\s*entry_id\s*=\s*`?([0-9a-fA-F-]+)`?"
    r"(?:\s*,\s*section_id\s*=\s*`?([^\s,`\-]+)`?)?"
    r"(?:\s*[-—–]\s*(.+?))?"
    r"\s*$",
    re.MULTILINE,
)


class ExportNotReadyError(Exception):
    """Raised when the conversation has not ended yet."""


class ConversationNotFoundError(Exception):
    pass


@dataclass(slots=True)
class CitationRef:
    marker: str
    entry_id: str
    section_id: str | None
    reason: str | None = None
    display_name: str | None = None
    file_id: str | None = None
    storage_key: str | None = None
    missing: bool = False


@dataclass(slots=True)
class ExportPlan:
    """Everything needed to materialise a conversation export.

    Members are documented in build_export_plan(). The route handler
    converts this into a streaming zip response.
    """
    conversation_id: str
    session_id: str
    started_at: str | None
    ended_at: str | None
    user_message: str
    agent_response: str
    citations: list[CitationRef] = field(default_factory=list)
    metadata_blobs: dict[str, dict[str, Any]] = field(default_factory=dict)


def parse_citations(agent_response: str) -> list[CitationRef]:
    """Extract `[^marker]: entry_id=...[, section_id=...]` footnotes.

    Multiple footnotes for the same entry_id are kept (the marker is
    distinct), but downstream zip packing dedups by entry_id when copying
    the actual file bytes."""
    if not agent_response:
        return []
    cites: list[CitationRef] = []
    seen_markers: set[str] = set()
    for m in _FOOTNOTE_RE.finditer(agent_response):
        marker = m.group(1)
        entry_id = m.group(2).strip()
        section_id = m.group(3).strip() if m.group(3) else None
        reason = m.group(4).strip() if m.group(4) else None
        if marker in seen_markers:
            continue
        seen_markers.add(marker)
        cites.append(CitationRef(
            marker=marker, entry_id=entry_id, section_id=section_id,
            reason=reason,
        ))
    return cites


async def build_export_plan(
    session: AsyncSession,
    *,
    conversation_id: str,
) -> ExportPlan:
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        raise ConversationNotFoundError(conversation_id)
    if conv.ended_at is None:
        raise ExportNotReadyError(
            f"conversation {conversation_id} has not ended yet"
        )

    plan = ExportPlan(
        conversation_id=conv.id,
        session_id=conv.session_id,
        started_at=conv.started_at.isoformat() if conv.started_at else None,
        ended_at=conv.ended_at.isoformat() if conv.ended_at else None,
        user_message=conv.user_message,
        agent_response=conv.agent_response or "",
        citations=parse_citations(conv.agent_response or ""),
    )

    # resolve live entry_ids referenced by citations
    entry_ids = list({c.entry_id for c in plan.citations})
    if entry_ids:
        rows = await entries_repo.list_live_with_file_by_ids(session, entry_ids)
        live_by_id = {e.id: (e, f) for e, f in rows}
        # Also fetch metadata blobs (in user-visible shape).
        for eid in entry_ids:
            if eid in live_by_id:
                try:
                    plan.metadata_blobs[eid] = await get_user_metadata(
                        session, entry_id=eid,
                    )
                except Exception:
                    plan.metadata_blobs[eid] = {"entry_id": eid, "error": "metadata_failed"}

        for cite in plan.citations:
            pair = live_by_id.get(cite.entry_id)
            if pair is None:
                cite.missing = True
                continue
            entry, file_row = pair
            cite.display_name = entry.display_name
            cite.file_id = file_row.id
            cite.storage_key = file_row.storage_key

    return plan


def render_manifest(plan: ExportPlan) -> dict[str, Any]:
    """Manifest JSON shape: stable, machine-readable summary of the export."""
    citations: list[dict[str, Any]] = []
    for c in plan.citations:
        citations.append({
            "marker": c.marker,
            "entry_id": c.entry_id,
            "section_id": c.section_id,
            "reason": c.reason,
            "display_name": c.display_name,
            "file_id": c.file_id,
            "missing": c.missing,
        })
    missing = [c.entry_id for c in plan.citations if c.missing]
    return {
        "conversation_id": plan.conversation_id,
        "session_id": plan.session_id,
        "started_at": plan.started_at,
        "ended_at": plan.ended_at,
        "user_message": plan.user_message,
        "citations": citations,
        "missing": missing,
    }


_INLINE_FOOTNOTE_RE = re.compile(
    r"^\[\^([^\]]+)\]:\s*entry_id\s*=.*$",
    re.MULTILINE,
)


def render_inline_markdown(plan: ExportPlan) -> str:
    """Single-file markdown: agent_response with citation footnotes
    rewritten to human-readable annotations.

    The agent stores footnotes as `[^a]: entry_id=01abcd... - reason`.
    For sharing or pasting into a notebook the raw entry_id is noise —
    this function rewrites each footnote to:

      [^a]: **Display name** — folder/path/  (entry 01abcdef)
            > one-line summary (if available)
            optional reason

    Missing entries fall back to "(unavailable)" so the footnote still
    renders rather than disappearing. The body text (`[^a]` markers) is
    untouched, so standard markdown footnote rendering still works.
    """
    body = plan.agent_response or ""
    if not plan.citations:
        return _markdown_header(plan) + body

    by_marker = {c.marker: c for c in plan.citations}

    def _replace(m: re.Match[str]) -> str:
        marker = m.group(1)
        cite = by_marker.get(marker)
        if cite is None:
            return m.group(0)
        return f"[^{marker}]: " + _format_citation(cite, plan.metadata_blobs)

    rewritten = _INLINE_FOOTNOTE_RE.sub(_replace, body)
    return _markdown_header(plan) + rewritten


def _markdown_header(plan: ExportPlan) -> str:
    """A small frontmatter-ish block so the exported file is readable
    standalone — the original conversation context isn't in the prose."""
    lines = ["---"]
    lines.append(f"conversation_id: {plan.conversation_id}")
    if plan.started_at:
        lines.append(f"started_at: {plan.started_at}")
    if plan.ended_at:
        lines.append(f"ended_at: {plan.ended_at}")
    lines.append("---\n")
    if plan.user_message:
        lines.append(f"**Question:** {plan.user_message.strip()}\n")
    return "\n".join(lines) + "\n"


def _format_citation(
    cite: CitationRef, metadata_blobs: dict[str, dict[str, Any]],
) -> str:
    if cite.missing:
        tail = f"  (entry {cite.entry_id[:8]} unavailable)"
        if cite.reason:
            tail += f" — {cite.reason}"
        return f"_(reference removed)_{tail}"
    name = cite.display_name or "(unnamed)"
    meta = metadata_blobs.get(cite.entry_id) or {}
    folder = meta.get("folder_path") or ""
    short = cite.entry_id[:8]
    head = f"**{name}** — `{folder}`  (entry {short})"
    pieces = [head]
    summary = (meta.get("summary") or "").strip()
    if summary:
        pieces.append(f"  > {summary}")
    if cite.section_id:
        pieces.append(f"  section: `{cite.section_id}`")
    if cite.reason:
        pieces.append(f"  {cite.reason.strip()}")
    return "\n".join(pieces)


def _safe_zip_name(name: str) -> str:
    """Strip path separators and control chars from a display_name so it's
    safe inside a zip without collisions across platforms."""
    out = []
    for ch in name:
        if ch in ("/", "\\", "\x00") or ord(ch) < 32:
            out.append("_")
        else:
            out.append(ch)
    return "".join(out) or "unnamed"


def reference_zip_paths(plan: ExportPlan) -> dict[str, tuple[str, str]]:
    """Map entry_id -> (zip_path_for_file, zip_path_for_metadata).

    Display-name collisions inside `references/` are disambiguated by
    appending the short entry id."""
    used: dict[str, str] = {}
    out: dict[str, tuple[str, str]] = {}
    seen_eids: set[str] = set()
    for cite in plan.citations:
        if cite.missing or not cite.display_name or cite.entry_id in seen_eids:
            continue
        seen_eids.add(cite.entry_id)
        base = _safe_zip_name(cite.display_name)
        if base in used and used[base] != cite.entry_id:
            short = cite.entry_id[:8]
            base = f"{cite.entry_id[:8]}_{base}"
        used[base] = cite.entry_id
        file_path = f"references/{base}"
        meta_path = f"references/{base}.metadata.json"
        out[cite.entry_id] = (file_path, meta_path)
    return out
