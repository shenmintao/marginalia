"""Stable context for the agent — DESIGN.md §10.2.

The two LLM phases of a turn use **two independent system prompts**, both
followed by the same snapshot block:

  - plan phase:    PLAN_PHASE_PROMPT  + snapshot
  - execute phase: EXECUTE_PHASE_PROMPT + snapshot

Mirrors kb-lite's split (PLANNING_PROMPT vs SYSTEM_PROMPT). Keeping the
phases' prompts disjoint prevents cross-contamination — the answer-shaped
rules (markdown layout, `[^a]` footnotes, citation discipline) only apply
in execute, and the plan contract (numbered plain-text or NO_PLAN) only
applies in plan. Earlier the two were fused into one `AGENT_IDENTITY`,
which let the planner write a full markdown answer in the plan slot and
let the executor inherit phantom plan-phase rules.

The snapshot suffix is identical between phases so cache hits still work
within a phase across turns; the divergent prefix means the two phases
each have their own cache budget — acceptable, both phases are short and
this is far cheaper than the leakage failure mode it replaces.

Journal recall is logically frozen for the duration of one session by
filtering `created_at < session.started_at`. This both:
  * excludes the session's own reflect_turn rows (which would otherwise
    fold the agent's just-written notes back into its next plan-phase
    prompt — a noisy self-loop, design [[journal-tiers]]), and
  * keeps the journal slice stable across turns, so the prefix doesn't
    drift mid-session.

V1: rebuilt on every turn (cheap; the underlying queries take a handful
of milliseconds). The catalog/views/tags slices are NOT logically frozen
— per DESIGN.md §4.2 the offline writers don't run during live sessions,
so in practice they don't drift.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.session import session_scope
from marginalia.llm.types import ChatMessage, ToolResultBlock, ToolUseBlock
from marginalia.repositories import sessions as session_service

from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories import views as views_repo


EXECUTE_PHASE_PROMPT = """You are Marginalia's online investigator.

Answer in the user's language unless they ask otherwise. First check your
journal for prior investigation paths, then use tools to gather evidence, then
give a concise Markdown answer.

Core rules:
- Be brief, evidence-based, and explicit about missing evidence. Do not fill
  gaps with generic outside knowledge.
- Use Markdown in every answer.
- Do not fabricate sources, tags, dates, numbers, quotes, or entry IDs.
- If no tools were called in this turn, do not use citation markers or
  footnote definitions.
- The snapshot below is only an index overview. It is not citable evidence and
  contains no valid `entry_id` values. Use tools for evidence.

Citations:
- Cite specific evidence with footnotes only when the cited `entry_id` came
  from a real tool result in this turn.
- Footnote format is strict ASCII:
  `[^a]: entry_id=<id>, quote="<10-60 verbatim chars>", page=<n> - <reason>`
- Required fields: `entry_id` and reason. Optional fields: `quote`, `page`.
  Field order is fixed: `entry_id`, `quote`, `page`, reason.
- Use `quote` whenever exact text is available. Escape `"` as `\\"` and `\\`
  as `\\\\`. Do not use multiple `quote=` fields or `+` concatenation.
- Use `page` only for PDFs and only when a tool returned a physical PDF page
  such as `[Page N]` or `page_start`. Prefer `quote`; printed page labels may
  be offset by covers or tables of contents.
- Use separate footnotes for separate evidence locations, even within the same
  entry.

Tool strategy:
- Start each substantive question with `search_journal`.
- For multi-keyword journal lookup, first try `search_journal(tags=[...])` for
  OR-style tag recall; if results are weak, fall back to
  `search_journal(text=...)`.
- Use `list_folder`, `search_metadata`, `read_entries_metadata`, and
  `read_files` to verify candidate entries before answering.
- Tool calls are budgeted; stop and answer when enough evidence is collected.

Never modify user files, folders, or entries. Never describe raw tool-call
mechanics to the user; present conclusions plus citations.
"""


PLAN_PHASE_PROMPT = """Make the internal plan for Marginalia's current turn.
No tools are available here; tools are available only in execute.

Output exactly one form, ending with a session title line:

1. `NO_PLAN: <1-2 short sentences in the user's language>`
   `Session name: <2-8 word title in the user's language>`
   Use only for greetings, thanks, pure small talk, meaningless tests, or
   clearly external realtime data such as weather, prices, or breaking news.
   Do not include citations, footnotes, headings, tables, or `entry_id=`.

2. A plain numbered plan, 3-5 lines, then:
   `<number>. <short execute-phase tool step>`
   `Session name: <2-8 word title in the user's language>`

Plan constraints:
- Start directly with `NO_PLAN: ` or `1. `.
- The final line must start exactly with `Session name: `.
- The session name should be concise, human-readable, and specific to this
  session's topic. Do not include quotes, Markdown, UUIDs, or `entry_id=`.
- No preamble, XML, code block, Markdown heading/table/list, citation marker,
  footnote definition, UUID, `entry_id=`, or user-facing answer.
- Do not answer from the snapshot. It is only an index overview; concrete facts
  must be verified with tools during execute.

Common paths:
- Prior work: `search_journal`.
- Multi-keyword journal lookup: `search_journal(tags=[...])` first, then
  `search_journal(text=...)` if needed.
- Candidate files: `search_metadata`, `list_folder`, `read_entries_metadata`,
  then `read_files`.
- Aggregation: `query_sql` / `query_log`.
"""


# Caps to keep the snapshot bounded.
TOP_LEVEL_CATALOGS_LIMIT = 50
VIEWS_LIMIT = 30
TAG_TOP_PER_FACET = 30
RECENT_JOURNAL_LIMIT = 10


async def build_stable_snapshot(
    db: AsyncSession, *, session_started_at: datetime,
) -> dict[str, Any]:
    """Build the structured snapshot the agent's stable system prompt
    embeds. Keep small + deterministic so prompt cache works.

    `session_started_at` freezes the journal slice to rows written before
    the current session began — see module docstring for rationale.
    """
    top_cats = await catalogs_repo.list_live_top_level(
        db, limit=TOP_LEVEL_CATALOGS_LIMIT,
    )
    cat_counts = await catalogs_repo.direct_entry_counts(db)
    catalog_view = [
        {
            "id": c.id,
            "name": c.name,
            "summary": c.summary,
            "doc_count": cat_counts.get(c.id, 0),
        }
        for c in top_cats
    ]

    views = await views_repo.list_for_snapshot(db, limit=VIEWS_LIMIT)
    view_view = [
        {"id": v.id, "name": v.name, "summary": v.summary}
        for v in views
    ]

    tags_by_facet: dict[str, list[dict[str, Any]]] = {}
    for facet in ("topic", "form", "time", "source", "language", "extra"):
        rows = await tags_repo.top_per_facet(
            db, facet, limit=TAG_TOP_PER_FACET,
        )
        if rows:
            tags_by_facet[facet] = [
                {"id": tid, "name": n, "doc_count": dc or 0}
                for tid, n, dc in rows
            ]

    # Logically frozen at session start — see module docstring.
    rows = await journal_repo.recent_journal_for_snapshot(
        db, before=session_started_at, limit=RECENT_JOURNAL_LIMIT,
    )
    # NOTE: journal row `id` is intentionally NOT exposed here. The model
    # was laundering it into fake `[^a]: entry_id=<journal-uuid>` footnotes,
    # which is misuse — entry_id must point at a catalog entry returned by
    # an actual search/list tool call, not a snapshot row id.
    journal_view = [
        {
            "kind": j.source_kind,
            "note": j.note or "",
            "entry_count": len(j.entry_ids or []),
            "tags": list(j.tags or []),
        }
        for j in rows
    ]

    return {
        "catalog_top_level": catalog_view,
        "views": view_view,
        "tags_by_facet": tags_by_facet,
        "recent_journal": journal_view,
    }


def render_system_prompt(
    snapshot: dict[str, Any],
    *,
    phase: Literal["plan", "execute"] = "execute",
) -> str:
    """Combine phase-specific identity + snapshot into one system prompt.

    Plan and execute use disjoint identity bodies (PLAN_PHASE_PROMPT vs
    EXECUTE_PHASE_PROMPT) so neither phase contaminates the other's
    instructions. The snapshot suffix is identical between phases.
    """
    head = PLAN_PHASE_PROMPT if phase == "plan" else EXECUTE_PHASE_PROMPT
    return (
        head
        + "\n\n# Current Knowledge Base Snapshot\n\n"
        + "```json\n"
        + json.dumps(snapshot, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


RESUME_BOUNDARY_NOTE = (
    "(The messages above replay earlier completed turns in this session. "
    "The next user message is the live new turn; continue the investigation "
    "and answer using the full conversation context.)"
)

# Cap for tool result text when replaying history — prevents a single
# massive result from blowing out the resumed prefix.
RESUME_MAX_TOOL_RESULT_LEN = 50_000


async def build_resumed_messages(
    session_id: str, *, current_conversation_id: str,
) -> list[ChatMessage]:
    """Reconstruct the LLM's prior conversation history for an open session.

    Replays every prior turn — user message, every tool_call/tool_result
    pair, and the final agent_response — so the executor sees the same
    context it would have during the original turns. Synthesizes fresh
    `tool_use_id`s per resumed turn (`tu_resume_<turn>_<idx>`); the model
    only needs ToolUse↔ToolResult ids to be self-consistent within one
    request, not stable across turns.

    Closes with a boundary note (system-note in user-role since the
    top-level `system` field is already pinned) so the model can distinguish
    replayed history from the live new turn.

    Shared between the execute phase and reflect_turn handler so both
    use the same prefix — enabling prompt-cache hits across the two
    LLM profiles when they share the same provider/model.
    """
    async with session_scope() as db:
        rows = await session_service.list_for_session_ordered(db, session_id)

    history: list[ChatMessage] = []
    for conv in rows:
        if conv.id == current_conversation_id:
            continue
        if not conv.user_message:
            continue
        history.append(ChatMessage(role="user", content=conv.user_message))

        tool_calls = [tc for tc in (conv.tool_calls or []) if isinstance(tc, dict)]
        if tool_calls:
            assistant_blocks: list = []
            tool_blocks: list[ToolResultBlock] = []
            for idx, tc in enumerate(tool_calls):
                tu_id = f"tu_resume_{conv.turn_index}_{idx}"
                assistant_blocks.append(ToolUseBlock(
                    id=tu_id,
                    name=str(tc.get("name") or "tool"),
                    arguments=dict(tc.get("arguments") or {}),
                ))
                result = tc.get("result")
                err = tc.get("error")
                if err:
                    body = f"[error] {err}"
                    is_error = True
                elif isinstance(result, dict):
                    try:
                        body = json.dumps(result, ensure_ascii=False)
                    except (TypeError, ValueError):
                        body = str(result)
                    is_error = False
                else:
                    body = str(result) if result is not None else ""
                    is_error = False
                if len(body) > RESUME_MAX_TOOL_RESULT_LEN:
                    body = body[:RESUME_MAX_TOOL_RESULT_LEN] + "\n…[truncated on resume]"
                tool_blocks.append(ToolResultBlock(
                    tool_call_id=tu_id, content=body, is_error=is_error,
                ))
            history.append(ChatMessage(role="assistant", content=assistant_blocks))
            history.append(ChatMessage(role="user", content=tool_blocks))

        if conv.agent_response:
            history.append(ChatMessage(
                role="assistant", content=conv.agent_response,
            ))

    if history:
        history.append(ChatMessage(role="user", content=RESUME_BOUNDARY_NOTE))
    return history
