"""Agent runtime — DESIGN.md §10.2 + §12.2.

Plan-Execute loop, exposed as async generator yielding AgentEvent frames
for SSE streaming. One `run_turn(session_id, user_message)` invocation:

  1. Open one conversation row (turn_index = next). Yield "conversation".
  2. Plan phase: yield "planning", do ONE LLM call with `tools=[]`,
     yield "plan" with full plan_text. Stored in conversations.llm_calls
     under phase='plan'. If plan_text starts with `NO_PLAN:` the trailing
     answer is treated as the final answer and execute is skipped.
  3. Execute phase: up to `settings.agent_execute_max_turns` (default 15)
     LLM calls. For each:
         - yield "thinking", LLM call (records usage)
         - if model returned tool_calls: yield "tool_call" per call,
           dispatch (with dedup + doom-loop guards), yield "tool_result",
           feed back as `tool` message
         - if model returned text + no tool_calls AND stop_reason='end_turn':
           yield "answer" with final text
         - if final text hits stop_reason='max_tokens', continue the answer
           server-side and emit one merged "answer" event.
     Once the run enters the last 1/3 of the budget, append wrap-up tail.
  4. Truncation: if the turn budget is hit, yield "answer" with fallback
     text and mark truncated=True.
  5. Finalize: write agent_response, ended_at; enqueue reflect_turn task
     (priority 30); record task_outcome; yield "done" with usage JSON.

Guards (added 2026-05-24, all append-only — never mutate prior messages
so ephemeral cache breakpoints stay valid):
  - NO_PLAN fast-path: planner can opt out of execute for trivial turns.
  - Tool-call dedup: identical (name, args) within one turn returns the
    prior result synthetically without re-dispatching.
  - Doom-loop guard: if the same (name, args) appears K times in the last
    N tool calls, the next tool result message gets a STOP nudge appended.

Concurrency: this runtime assumes one in-flight turn per session. The
HTTP route layer (api/routes_chat.py) enforces this with a per-session
asyncio.Lock held for the whole SSE stream; the cross-process backstop
is `UNIQUE(session_id, turn_index)` on `conversations`. Anything else
calling `run_turn` directly (tests, scripts) MUST serialise per session
or risk duplicate-row IntegrityError on the second writer.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator

from marginalia.agent.stable_context import (
    build_resumed_messages,
    build_stable_snapshot,
    render_system_prompt,
)
from marginalia.agent.tools import ToolContext, all_tool_defs, get_tool
from marginalia.agent.types import AgentEvent, AgentTurnError, TurnUsage
from marginalia.db.models import Session as SessionRow
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    get_chat_client,
)
from marginalia.config import get_settings
from marginalia.pipelines.pdf_text import (
    PdfTextRange,
    first_page_number,
    get_pdf_page_labels_for_file,
    get_pdf_text_for_file,
    locate_quote_page,
    resolve_page_label,
)
from marginalia.repositories import sessions as session_service
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import tags as tags_repo
from marginalia.repositories import folders as folders_repo
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories.task_outcomes import record_outcome
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_REFLECT_TURN
from marginalia.agent import tool_display

log = logging.getLogger(__name__)

MAX_TOOL_RESULT_LEN = 50_000
# Structured-truncation safety net: how many trim passes before falling
# back to string slicing. Practically each pass halves one large list, so
# 3 passes can absorb three different oversize lists in one payload.
STRUCTURED_TRUNCATE_PASSES = 3
# Default token budgets — overridable per-deploy via AGENT_PLAN_MAX_TOKENS /
# AGENT_EXECUTE_MAX_TOKENS in settings. Sized for gpt-4o-class models; bump
# for long-context backends (DeepSeek-V3, Claude 3.5 Sonnet, etc.).
PLAN_MAX_TOKENS = 1024
EXECUTE_MAX_TOKENS = 2048
TOOL_RESULT_PREVIEW_LEN = 240

NO_PLAN_PREFIX = "NO_PLAN:"
SESSION_NAME_PREFIX = "Session name:"
MAX_SESSION_NAME_LEN = 80

# Doom-loop: if the same (name, canonical_args) shows up
# DOOM_LOOP_THRESHOLD times within the last DOOM_LOOP_WINDOW tool calls,
# inject a STOP nudge. The threshold is one above the dedup floor — dedup
# already neutralises duplicate work, so this fires only on near-duplicate
# patterns the model is iterating on (slightly different args each time).
DOOM_LOOP_WINDOW = 6
DOOM_LOOP_THRESHOLD = 3
DOOM_LOOP_NUDGE = (
    "[runtime guard] You have repeatedly called the same tool with similar "
    "arguments. Stop expanding tool calls and give the final answer from the "
    "results already collected."
)
FINAL_ANSWER_CONTINUE_NUDGE = (
    "[runtime guard] Your previous final answer was cut off by the token "
    "limit. Continue exactly where it stopped. Do not restart, do not repeat "
    "previous text, do not call tools, and finish the answer."
)


def _canonical_args(arguments: Any) -> str:
    """Stable JSON serialisation of tool arguments for dedup keying.

    `sort_keys=True` so {a:1,b:2} and {b:2,a:1} hash identical; we accept
    that nested-dict ordering still collapses correctly because json.dumps
    recursively sorts. None-valued fields keep their slot — different from
    "field absent" — to avoid false dedup of intentionally-distinct calls.
    """
    try:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return repr(arguments)


def _find_largest_list(payload: Any) -> tuple[list, str, int] | None:
    """Walk `payload` and return the longest list with its dotted path and
    serialized weight, or None if the payload contains no lists. We pick by
    serialized character cost — a list of 5 huge dicts outranks a list of
    500 ints — because that's what the budget actually constrains."""
    best: tuple[list, str, int] | None = None

    def visit(node: Any, path: str) -> None:
        nonlocal best
        if isinstance(node, list):
            try:
                weight = len(json.dumps(node, ensure_ascii=False))
            except (TypeError, ValueError):
                weight = sum(len(repr(x)) for x in node)
            if best is None or weight > best[2]:
                best = (node, path or "$", weight)
            for i, item in enumerate(node):
                visit(item, f"{path}[{i}]")
        elif isinstance(node, dict):
            for k, v in node.items():
                visit(v, f"{path}.{k}" if path else k)

    visit(payload, "")
    return best


def _trim_largest_list(payload: Any, budget: int) -> tuple[bool, str | None, int]:
    """Find the largest list in `payload` and shrink it (in place) until the
    re-serialized payload fits within `budget`. Returns
    (changed, path, dropped_count). If no list is found or trimming cannot
    bring the payload under budget, returns (False, None, 0)."""
    target = _find_largest_list(payload)
    if target is None:
        return False, None, 0
    lst, path, _ = target
    original = list(lst)
    n = len(original)
    if n == 0:
        return False, None, 0
    # Binary-search the largest prefix that fits. lst is mutated in place,
    # so we save `original` once and restore the prefix on each probe.
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lst.clear()
        lst.extend(original[:mid])
        try:
            size = len(json.dumps(payload, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            size = budget + 1
        if size <= budget:
            lo = mid
        else:
            hi = mid - 1
    lst.clear()
    lst.extend(original[:lo])
    dropped = n - lo
    return dropped > 0, path, dropped


def _copy_jsonish(value: Any) -> Any:
    """Return a mutation-safe copy for model-only truncation."""
    try:
        return copy.deepcopy(value)
    except Exception:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return value


def _structured_truncate(payload: Any, budget: int) -> tuple[str, dict | None]:
    """Serialize `payload` to JSON ≤ `budget` chars by trimming its largest
    lists. Returns (json_text, marker) where `marker` describes what was
    dropped (or None when nothing was trimmed). Falls back to a string
    slice on the serialized output if structured passes can't shrink it
    enough — that branch should be rare and signals an oddly-shaped
    payload (deeply nested scalars, no lists)."""
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(payload)[:budget], None
    if len(text) <= budget:
        return text, None
    if not isinstance(payload, (dict, list)):
        return text[:budget] + "...(truncated)", {
            "_truncated_field": "$", "_truncated_dropped": -1,
            "_truncated_reason": "non-container payload",
        }
    # Reserve headroom for the marker we'll inject after trimming, so the
    # final post-marker payload still fits within `budget`.
    MARKER_HEADROOM = 240
    inner_budget = max(budget - MARKER_HEADROOM, budget // 2)
    truncations: list[dict[str, Any]] = []
    for _ in range(STRUCTURED_TRUNCATE_PASSES):
        changed, path, dropped = _trim_largest_list(payload, inner_budget)
        if not changed:
            break
        truncations.append({"path": path, "dropped": dropped})
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            break
        if len(text) <= inner_budget:
            break
    marker: dict[str, Any] = {}
    if truncations:
        first = truncations[0]
        marker["_truncated_field"] = first["path"]
        marker["_truncated_dropped"] = first["dropped"]
        if len(truncations) > 1:
            marker["_truncated_path"] = truncations
    if isinstance(payload, dict) and marker:
        payload.update(marker)
    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        pass
    if len(text) > budget:
        text = text[:budget] + "...(truncated)"
        marker.setdefault("_truncated_reason", "fallback string slice")
    return text, (marker or None)


@dataclass(slots=True)
class _CallGuard:
    """Per-turn tracker for dedup + doom-loop detection."""
    seen: dict[str, str] = field(default_factory=dict)  # key -> prior result_text
    seen_previews: dict[str, str] = field(default_factory=dict)  # key -> user-facing preview
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=DOOM_LOOP_WINDOW))
    nudged: bool = False

    def key(self, name: str, arguments: Any) -> str:
        return f"{name}::{_canonical_args(arguments)}"

    def remember(self, key: str, result_text: str, preview: str = "") -> None:
        self.seen[key] = result_text
        if preview:
            self.seen_previews[key] = preview
        self.recent.append(key)

    def is_duplicate(self, key: str) -> bool:
        return key in self.seen

    def should_nudge(self, key: str) -> bool:
        """True the *first* time the loop pattern crosses threshold.

        We count `key` in the rolling window but don't include the current
        call yet (caller decides whether to record it). Once nudged, never
        nudges again in the same turn — one warning is enough; piling on
        wastes tokens and pollutes the next prefix-cache hit.
        """
        if self.nudged:
            return False
        return self.recent.count(key) + 1 >= DOOM_LOOP_THRESHOLD


@dataclass(slots=True)
class _ExecuteOutcome:
    """Mutable carrier returned by `_run_execute_phase` so the caller can
    pick up the final answer text and truncation flag without needing a
    sentinel event in the public stream."""
    answer: str = ""
    truncated: bool = False


async def run_turn(
    *,
    session_id: str,
    user_message: str,
) -> AsyncIterator[AgentEvent]:
    """Run one user turn as an event stream.

    Yields AgentEvent frames covering the full plan-execute lifecycle.
    See AgentEvent docstring for event_type semantics.
    """
    if not user_message.strip():
        raise AgentTurnError("user_message is empty")

    async with session_scope() as db:
        last = await session_service.latest_turn_index(db, session_id)
        # Explicit None check — `last or -1` would treat turn_index 0 as
        # falsy and re-issue 0 for the second turn, colliding with the
        # UNIQUE(session_id, turn_index) constraint. (Long-standing bug
        # masked by the previous non-unique index; second turns silently
        # overwrote turn 0 in any read that joined on (session, turn).)
        turn_index = 0 if last is None else last + 1

        conv = await session_service.start_conversation(
            db, session_id=session_id, turn_index=turn_index,
            user_message=user_message,
        )
        # Need session.started_at to freeze the journal slice in the
        # snapshot — see stable_context module docstring.
        session_row = await db.get(SessionRow, session_id)
        if session_row is None:
            raise AgentTurnError(f"session {session_id!r} not found")
        snapshot = await build_stable_snapshot(
            db, session_started_at=session_row.started_at,
        )
        await db.commit()
        conversation_id = conv.id

    yield AgentEvent(event_type="conversation", data=conversation_id)

    # Two disjoint prompts (kb-lite-style). Each phase only sees the rules
    # that apply to it, so plan can't be tempted to write a markdown answer
    # under "must always use [^a] footnotes" instructions.
    plan_system = render_system_prompt(snapshot, phase="plan")
    execute_system = render_system_prompt(snapshot, phase="execute")
    chat = get_chat_client("chat")

    yield AgentEvent(event_type="planning")
    plan_text = await _run_plan_phase(
        chat=chat,
        system_prompt=plan_system,
        user_message=user_message,
        conversation_id=conversation_id,
    )
    session_name = _extract_session_name(plan_text)
    if session_name:
        await _store_session_name(session_id, session_name)
    yield AgentEvent(event_type="plan", data=plan_text)

    outcome = _ExecuteOutcome()
    no_plan_answer = _extract_no_plan_answer(plan_text)
    plan_for_execute = _strip_session_name_line(plan_text)
    if no_plan_answer is not None:
        # Planner declared the user's turn is trivial — skip execute,
        # still emit one fake "thinking" so the SSE stream shape stays
        # consistent for clients, and an "answer" with the planner's text.
        outcome.answer = no_plan_answer
        yield AgentEvent(
            event_type="answer",
            data=await _rewrite_footnotes_for_display(no_plan_answer),
        )
    else:
        # Resume: replay every prior turn into the executor's message
        # tape so it sees the full session arc, not just the current
        # question. Plan phase stays history-free — it's about scoping
        # this turn, not remembering past ones.
        resumed_history = await build_resumed_messages(
            session_id, current_conversation_id=conversation_id,
        )
        async for ev in _run_execute_phase(
            chat=chat,
            system_prompt=execute_system,
            plan_text=plan_for_execute,
            user_message=user_message,
            conversation_id=conversation_id,
            session_id=session_id,
            outcome=outcome,
            resumed_history=resumed_history,
        ):
            yield ev

    async with session_scope() as db:
        await session_service.finalize_conversation(
            db,
            conversation_id=conversation_id,
            agent_response=outcome.answer,
        )
        # NO_PLAN turns are trivial by definition (greetings, "thanks",
        # tiny pleasantries the planner answered directly with zero tool
        # calls). Reflecting them produces noisy journal entries that
        # crowd out real investigations and burn one reflect-LLM call per
        # turn for no signal. Skip the enqueue and mark the outcome.
        if no_plan_answer is None:
            await enqueue(
                db,
                kind=KIND_REFLECT_TURN,
                payload={"conversation_id": conversation_id},
                dedup_key=f"reflect_turn:{conversation_id}",
            )
        await record_outcome(
            db,
            task_kind="run_turn",
            object_kind="conversation",
            object_id=conversation_id,
            outcome="deferred" if outcome.truncated else "applied",
            detail={
                "turn_index": turn_index,
                "session_id": session_id,
                "truncated": outcome.truncated,
                "no_plan": no_plan_answer is not None,
            },
        )
        conv = await session_service.get_conversation(db, conversation_id)
        usage = TurnUsage(
            input_tokens=conv.total_input_tokens or 0,
            output_tokens=conv.total_output_tokens or 0,
            cache_read_tokens=conv.total_cache_read or 0,
            tool_calls=conv.total_tool_calls or 0,
            llm_calls=conv.total_llm_calls or 0,
            duration_ms=conv.total_duration_ms or 0,
            cost_estimate=conv.total_cost_estimate or Decimal("0"),
        )
        await db.commit()

    yield AgentEvent(
        event_type="done",
        data=json.dumps({
            "session_id": session_id,
            "conversation_id": conversation_id,
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
            "cache_read": usage.cache_read_tokens,
            "tool_calls": usage.tool_calls,
            "llm_calls": usage.llm_calls,
            "duration_ms": usage.duration_ms,
            "truncated": outcome.truncated,
            "session_name": session_name,
        }),
    )


# ---- plan -----------------------------------------------------------------

def _extract_session_name(plan_text: str) -> str | None:
    """Return the planner-supplied session title from the final plan line."""
    if not plan_text:
        return None
    for line in reversed(plan_text.splitlines()):
        text = line.strip()
        if not text:
            continue
        if not text.lower().startswith(SESSION_NAME_PREFIX.lower()):
            return None
        raw = text[len(SESSION_NAME_PREFIX):].strip()
        title = _clean_session_name(raw)
        return title or None
    return None


def _strip_session_name_line(plan_text: str) -> str:
    """Remove the final session-name control line before execute consumes it."""
    if not plan_text:
        return plan_text
    lines = plan_text.splitlines()
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx >= 0 and lines[idx].strip().lower().startswith(SESSION_NAME_PREFIX.lower()):
        del lines[idx]
    return "\n".join(lines).strip()


def _clean_session_name(raw: str) -> str:
    title = raw.strip().strip("`\"'")
    title = re.sub(r"\s+", " ", title)
    title = re.sub(r"^\s*[-*#]+\s*", "", title)
    if "entry_id=" in title or re.search(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b", title):
        return ""
    return title[:MAX_SESSION_NAME_LEN].rstrip()


async def _store_session_name(session_id: str, session_name: str) -> None:
    try:
        async with session_scope() as db:
            await session_service.update_session_name(
                db, session_id=session_id, name=session_name,
            )
            await db.commit()
    except Exception:
        log.exception("failed to store session name for session %s", session_id)


def _extract_no_plan_answer(plan_text: str) -> str | None:
    """Return the trailing answer if `plan_text` is a NO_PLAN fast-path.

    Tolerates leading whitespace and any minor formatting the model puts
    around the marker. Returns None if this is a normal plan (the common
    path), so the caller falls through to execute.
    """
    if not plan_text:
        return None
    stripped = plan_text.lstrip()
    if not stripped.startswith(NO_PLAN_PREFIX):
        return None
    stripped = _strip_session_name_line(stripped)
    answer = stripped[len(NO_PLAN_PREFIX):].strip()
    # Empty answer body is treated as a non-decision — fall back to execute
    # rather than returning a blank response to the user.
    return answer or None


def _strip_leaked_no_plan(answer: str) -> str:
    """Belt-and-suspenders for a model that mistakenly prefixes its
    execute-phase final answer with the NO_PLAN: control marker. The marker
    is plan-only — never user-visible — so we strip it here regardless of
    what the model emitted."""
    if not answer:
        return answer
    stripped = answer.lstrip()
    if stripped.startswith(NO_PLAN_PREFIX):
        return stripped[len(NO_PLAN_PREFIX):].lstrip()
    return answer


def _joined_final_answer(parts: list[str], fallback: str = "(no answer)") -> str:
    """Join final-answer fragments from max_tokens continuation calls."""
    text = "".join(p for p in parts if p)
    return _strip_leaked_no_plan(text or fallback)


def _cap_final_answer(answer: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0 or len(answer) <= max_chars:
        return answer, False
    return answer[:max_chars].rstrip(), True


async def _run_plan_phase(
    *,
    chat,
    system_prompt: str,
    user_message: str,
    conversation_id: str,
) -> str:
    started = time.monotonic()
    resp = await chat.complete(ChatRequest(
        system=system_prompt,
        messages=[ChatMessage(role="user", content=user_message)],
        max_tokens=get_settings().agent_plan_max_tokens,
        tools=None,            # Plan phase: zero tools (design §10.2).
        json_schema=None,
        temperature=0.3,
    ))
    duration_ms = int((time.monotonic() - started) * 1000)
    plan_text = resp.text or ""
    async with session_scope() as db:
        await session_service.append_llm_call(
            db,
            conversation_id=conversation_id,
            phase="plan",
            model=getattr(chat, "model", "?"),
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=resp.usage.cache_read_tokens,
            cache_creation_tokens=resp.usage.cache_creation_tokens,
            duration_ms=duration_ms,
            extra={"plan_text": plan_text},
        )
        await db.commit()
    return plan_text


# ---- live-render footnote rewrite ----------------------------------------

# Agent emits citation defs as:
#     [^a]: entry_id=<id>, quote="<verbatim excerpt>" - reason
#     [^a]: entry_id=<id>, page=<n> - reason
#
# The GUI deep-links via `?q=<urlencoded>` for quote-bearing footnotes (a
# DOM text search highlights the match) and `?page=<n>` for PDFs (the
# browser PDF viewer scrolls). Legacy fields (`lines=`, `section_id=`,
# descriptive `lines=...`) are still tolerated by the regex so historical
# turns don't crash on replay/export, but they don't produce any query
# string — the link opens the file without a jump.
#
# `<id>` accepts a full uuid or a hex-only short prefix (>= 8 chars).
# Backticks around the id / page / quote are tolerated. Quote bodies use
# `\"` and `\\` for embedded `"` and `\`.
#
# Defence in depth against LLM-emitted variants the prompt forbids but
# can still slip through: the field separator accepts `，` (中文逗号) as
# well as `,`; a quote may be followed by extra `+ "..."` segments
# (consumed but ignored — the URL can only carry one quote, so the GUI
# jumps to the first); and a value may be trailed by a parenthetical
# annotation in either ASCII or full-width brackets, e.g.
# `page=54（第54页）` (also consumed and ignored). We could not parse
# these in `runtime.py` and the entire footnote definition would leak
# unrendered to the user.
_LIVE_FOOTNOTE_RE = re.compile(
    r"^\[\^([^\]]+)\]:\s*entry_id\s*=\s*`?"
    r"([0-9a-fA-F][0-9a-fA-F\-]{6,35})`?"
    r"(?:\s*[,，]\s*(?:"
    r'quote\s*=\s*"((?:[^"\\]|\\.)*)"'                  # group 3: quote
    r'(?:\s*\+\s*"(?:[^"\\]|\\.)*")*'                   # extra `+ "..."` segments: tolerated, ignored
    r"|page\s*=\s*`?(?:([0-9]+(?:-[0-9]+)?)|(?:N/?A|n/?a|NA|na|none|null|unknown))`?"
    r"|lines?\s*=\s*`?\S+`?"                             # legacy lines: tolerated
    r"|section_id\s*=\s*`?[^\s,`]+`?"                   # legacy section_id: tolerated
    r")"
    r"(?:\s*[(（][^)）]*[)）])?"             # optional trailing (...) / （...） annotation
    r")*"
    r"(?:\s+[(（][^)）]*[)）])?"
    r"(?:\s*[-—–]\s*(.+?))?"
    r"\s*$",
    re.MULTILINE,
)


def _unescape_quote(s: str) -> str:
    return s.replace(r"\"", '"').replace(r"\\", "\\")


# Kinds whose FileViewer body is DOM-rendered text — the in-page text
# search behind `?q=<text>` actually scrolls + highlights on these. PDFs
# render in an `<iframe>` that only honours `#page=N`, so they're handled
# separately by mime/extension below.
_TEXT_SEARCHABLE_KINDS = frozenset({"text", "code", "log", "docx"})
_TEXT_SEARCHABLE_EXTS = frozenset({
    "txt", "md", "markdown", "rst", "log", "csv", "tsv",
    "json", "yaml", "yml", "toml", "ini", "conf", "env",
    "sql", "html", "css", "scss", "ts", "tsx", "js", "jsx",
    "py", "rb", "go", "rs", "java", "c", "h", "cpp", "hpp",
    "sh", "bash", "zsh", "ps1", "docx",
})


def _is_pdf_file(file: Any) -> bool:
    if file is None:
        return False
    mime = (getattr(file, "mime_type", None) or "").lower()
    ext = (getattr(file, "original_ext", None) or "").lower().lstrip(".")
    return mime == "application/pdf" or ext == "pdf"


def _pick_query_string(
    file: Any,
    quote: str | None,
    page: str | None,
    *,
    located_pdf_page: int | None = None,
) -> str:
    """Decide the locator query string from (file_type, quote, page).

    File type wins over what the LLM wrote: a PDF emits `?page=N` using
    the quote-located physical page when available, a text-shaped file
    emits `?q=<quote>` (or bare), and everything else (images, tables,
    audio) emits a bare link. This means the LLM doesn't have to choose
    between fields by file type — it can write both `quote="..."` and
    `page=N` and the backend keeps whichever is meaningful for this entry.
    """
    if file is None:
        return ""
    if _is_pdf_file(file):
        if located_pdf_page:
            return f"?page={located_pdf_page}"
        first = first_page_number(page)
        return f"?page={first}" if first else ""
    mime = (getattr(file, "mime_type", None) or "").lower()
    ext = (getattr(file, "original_ext", None) or "").lower().lstrip(".")
    kind = (getattr(file, "kind", None) or "").lower()
    is_text_searchable = (
        kind in _TEXT_SEARCHABLE_KINDS
        or ext in _TEXT_SEARCHABLE_EXTS
        or mime.startswith("text/")
    )
    if is_text_searchable and quote:
        return f"?q={urllib.parse.quote_plus(_unescape_quote(quote))}"
    return ""


async def _locate_pdf_quote_page(
    file: Any,
    quote: str,
    *,
    pages_cache: dict[str, PdfTextRange] | None = None,
) -> int | None:
    storage_key = getattr(file, "storage_key", None)
    if not storage_key or not quote.strip():
        return None
    try:
        cache_key = str(storage_key)
        if pages_cache is not None and cache_key in pages_cache:
            doc = pages_cache[cache_key]
        else:
            from marginalia.storage import get_storage

            storage = get_storage()
            doc = await get_pdf_text_for_file(storage, file)
            if pages_cache is not None:
                pages_cache[cache_key] = doc
    except Exception:
        log.exception("footnote rewrite: PDF quote locator failed")
        return None

    return locate_quote_page(doc, quote)


async def _resolve_pdf_page_locator(file: Any, page: str | None) -> int | None:
    first = first_page_number(page)
    if first is None:
        return None
    try:
        from marginalia.storage import get_storage

        labels = await get_pdf_page_labels_for_file(get_storage(), file)
        return resolve_page_label(labels, first) or first
    except Exception:
        log.exception("footnote rewrite: PDF page-label lookup failed")
        return first


async def _rewrite_footnotes_for_display(answer: str) -> str:
    """Resolve `[^a]: entry_id=<uuid>, quote="...", page=N - reason` defs to
    `[^a]: [name](entry:<id>?q=...|?page=N) — reason` for live SSE rendering.

    The persisted `agent_response` keeps the raw form so downstream exports
    still parse. Missing/ambiguous ids fall back to `(entry <short> unavailable)`.
    Legacy `lines=`/`section_id=` fields are tolerated but don't produce a
    deep-link query string. Locator selection (page vs quote vs bare) is
    driven by the entry's actual file type — see [[_pick_query_string]].
    """
    if not answer or "entry_id" not in answer:
        return answer
    matches = list(_LIVE_FOOTNOTE_RE.finditer(answer))
    if not matches:
        return answer

    raw_ids = list({m.group(2).strip() for m in matches})
    name_by_id: dict[str, str] = {}
    file_by_id: dict[str, Any] = {}
    resolved: dict[str, str] = {}
    try:
        async with session_scope() as db:
            for raw in raw_ids:
                full, err = await entries_repo.resolve_entry_id_prefix(db, raw)
                if err is None:
                    resolved[raw] = full
            if resolved:
                rows = await entries_repo.list_live_with_file_by_ids(
                    db, list(set(resolved.values())),
                )
                name_by_id = {entry.id: entry.display_name for entry, _ in rows}
                file_by_id = {entry.id: file for entry, file in rows}
    except Exception:
        log.exception("footnote rewrite: entry lookup failed; keeping raw form")
        return answer

    located_pdf_pages: dict[int, int] = {}
    pdf_pages_cache: dict[str, PdfTextRange] = {}
    for m in matches:
        raw_eid = m.group(2).strip()
        full_eid = resolved.get(raw_eid, raw_eid)
        file = file_by_id.get(full_eid)
        quote = m.group(3)
        page = (m.group(4) or "").strip() or None
        if not _is_pdf_file(file):
            continue
        located = None
        if quote:
            located = await _locate_pdf_quote_page(
                file, quote, pages_cache=pdf_pages_cache,
            )
        if located is None and page:
            located = await _resolve_pdf_page_locator(file, page)
        if located:
            located_pdf_pages[m.start()] = located

    def _replace(m: re.Match[str]) -> str:
        marker = m.group(1)
        raw_eid = m.group(2).strip()
        quote = m.group(3)
        page = (m.group(4) or "").strip() or None
        reason = m.group(5).strip() if m.group(5) else None

        full_eid = resolved.get(raw_eid, raw_eid)
        short = full_eid[:8]
        name = name_by_id.get(full_eid)
        if name is None:
            head = f"(entry {short} unavailable)"
        else:
            qs = _pick_query_string(
                file_by_id.get(full_eid),
                quote,
                page,
                located_pdf_page=located_pdf_pages.get(m.start()),
            )
            head = f"[{name}](entry:{full_eid}{qs})"
        if reason:
            return f"[^{marker}]: {head} — {reason}"
        return f"[^{marker}]: {head}"

    return _LIVE_FOOTNOTE_RE.sub(_replace, answer)



# ---- execute --------------------------------------------------------------

async def _run_execute_phase(
    *,
    chat,
    system_prompt: str,
    plan_text: str,
    user_message: str,
    conversation_id: str,
    session_id: str,
    outcome: _ExecuteOutcome,
    resumed_history: list[ChatMessage] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Execute loop as event stream.

    Yields AgentEvent frames: thinking / tool_call / tool_result / answer.
    Truncation status and final-answer text are written into `outcome`
    instead of mixed into the stream — keeps the public event stream
    clean (no internal sentinels) and lets the caller branch on plain
    Python attributes.

    `resumed_history` (when present) is the replayed prior-turns context
    built by `build_resumed_messages` and is prepended ahead of the
    current turn's user message, with a boundary note baked in by the
    builder.
    """
    tool_defs = all_tool_defs()
    ctx = ToolContext(session_id=session_id, conversation_id=conversation_id)
    guard = _CallGuard()

    messages: list[ChatMessage] = list(resumed_history or []) + [
        ChatMessage(role="user", content=user_message),
        ChatMessage(role="assistant", content=(
            "Plan prepared:\n" + (plan_text or "(no specific plan; answer directly)")
        )),
    ]

    settings = get_settings()
    max_execute_turns = max(3, settings.agent_execute_max_turns)
    max_final_continuations = max(0, settings.agent_final_answer_continue_turns)
    max_final_chars = max(0, settings.agent_final_answer_max_chars)
    max_total_turns = max_execute_turns + max_final_continuations

    last_text: str | None = None
    final_parts: list[str] = []
    final_continuations = 0
    continuing_final_answer = False

    for turn in range(max_total_turns):
        if turn >= max_execute_turns and not continuing_final_answer:
            break

        budget_tail = (
            None
            if continuing_final_answer
            else _budget_tail(turn=turn, limit=max_execute_turns)
        )
        loop_messages = messages + [
            ChatMessage(role="user", content=budget_tail)
        ] if budget_tail else messages
        request_tools = None if continuing_final_answer else tool_defs

        yield AgentEvent(event_type="thinking")

        started = time.monotonic()
        resp = await chat.complete(ChatRequest(
            system=system_prompt,
            messages=loop_messages,
            max_tokens=settings.agent_execute_max_tokens,
            tools=request_tools,
            tool_choice="none" if continuing_final_answer else "auto",
            json_schema=None,
            temperature=0.3,
        ))
        duration_ms = int((time.monotonic() - started) * 1000)

        async with session_scope() as db:
            await session_service.append_llm_call(
                db,
                conversation_id=conversation_id,
                phase="execute",
                model=getattr(chat, "model", "?"),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cache_read_tokens=resp.usage.cache_read_tokens,
                cache_creation_tokens=resp.usage.cache_creation_tokens,
                duration_ms=duration_ms,
                extra={
                    "execute_turn": turn,
                    "stop_reason": resp.stop_reason,
                    "final_continuation": continuing_final_answer,
                    "final_continuation_index": final_continuations
                    if continuing_final_answer else None,
                },
            )
            await db.commit()

        if resp.tool_calls and not continuing_final_answer:
            assistant_blocks: list = []
            if resp.text:
                assistant_blocks.append(TextBlock(text=resp.text))
            for tc in resp.tool_calls:
                assistant_blocks.append(ToolUseBlock(
                    id=tc.id, name=tc.name, arguments=tc.arguments,
                ))
            messages.append(ChatMessage(role="assistant", content=assistant_blocks))

            tool_result_blocks: list[ToolResultBlock] = []
            async for ev in _dispatch_tool_calls(
                tool_calls=resp.tool_calls,
                ctx=ctx,
                conversation_id=conversation_id,
                result_blocks=tool_result_blocks,
                guard=guard,
            ):
                yield ev
            messages.append(ChatMessage(role="tool", content=tool_result_blocks))
            last_text = resp.text or last_text
            continue

        if resp.text:
            last_text = resp.text
        if continuing_final_answer or final_parts:
            if resp.text:
                final_parts.append(resp.text)
            answer = _joined_final_answer(final_parts, last_text or "(no answer)")
            answer, capped = _cap_final_answer(answer, max_final_chars)
            if capped:
                log.warning(
                    "conversation %s final answer hit max char cap=%d",
                    conversation_id,
                    max_final_chars,
                )
                outcome.truncated = True
                outcome.answer = answer
                yield AgentEvent(
                    event_type="answer",
                    data=await _rewrite_footnotes_for_display(answer),
                )
                return
            if resp.stop_reason in ("end_turn", "stop_sequence"):
                outcome.answer = answer
                yield AgentEvent(
                    event_type="answer",
                    data=await _rewrite_footnotes_for_display(answer),
                )
                return
            if resp.stop_reason == "max_tokens":
                if final_continuations >= max_final_continuations:
                    log.warning(
                        "conversation %s final answer hit continuation limit=%d",
                        conversation_id,
                        max_final_continuations,
                    )
                    outcome.truncated = True
                    outcome.answer = answer
                    yield AgentEvent(
                        event_type="answer",
                        data=await _rewrite_footnotes_for_display(answer),
                    )
                    return
                final_continuations += 1
                if resp.text:
                    messages.append(ChatMessage(role="assistant", content=resp.text))
                messages.append(ChatMessage(
                    role="user",
                    content=FINAL_ANSWER_CONTINUE_NUDGE,
                ))
                continuing_final_answer = True
                continue

            log.warning(
                "conversation %s final continuation stopped with %s",
                conversation_id,
                resp.stop_reason,
            )
            outcome.answer = answer
            yield AgentEvent(
                event_type="answer",
                data=await _rewrite_footnotes_for_display(answer),
            )
            return

        if resp.stop_reason in ("end_turn", "stop_sequence"):
            answer = _strip_leaked_no_plan(resp.text or last_text or "(no answer)")
            outcome.answer = answer
            yield AgentEvent(
                event_type="answer",
                data=await _rewrite_footnotes_for_display(answer),
            )
            return
        if resp.stop_reason == "max_tokens":
            final_parts.append(resp.text or last_text or "")
            answer = _joined_final_answer(final_parts, last_text or "(no answer)")
            answer, capped = _cap_final_answer(answer, max_final_chars)
            if capped or max_final_continuations <= 0:
                if capped:
                    log.warning(
                        "conversation %s final answer hit max char cap=%d",
                        conversation_id,
                        max_final_chars,
                    )
                else:
                    log.warning(
                        "conversation %s hit max_tokens with continuation disabled",
                        conversation_id,
                    )
                outcome.truncated = True
                outcome.answer = answer
                yield AgentEvent(
                    event_type="answer",
                    data=await _rewrite_footnotes_for_display(answer),
                )
                return
            final_continuations += 1
            if resp.text:
                messages.append(ChatMessage(role="assistant", content=resp.text))
            messages.append(ChatMessage(
                role="user",
                content=FINAL_ANSWER_CONTINUE_NUDGE,
            ))
            continuing_final_answer = True
            continue

    log.warning("conversation %s hit agent_execute_max_turns=%d", conversation_id,
                max_execute_turns)
    fallback = _strip_leaked_no_plan(
        last_text
        or "This investigation exceeded the turn budget before a complete answer was produced. Please narrow the question or try another angle."
    )
    outcome.truncated = True
    outcome.answer = fallback
    yield AgentEvent(
        event_type="answer",
        data=await _rewrite_footnotes_for_display(fallback),
    )


def _budget_tail(*, turn: int, limit: int) -> str | None:
    """Return the budget tail message for execute turn `turn` (0-indexed).

    Always show 'rounds used / left'. Once the run enters the last third of
    `limit`, append a wrap-up nudge so the agent stops gathering and writes
    the answer.
    """
    used = turn  # turns already consumed before this call
    left = limit - used
    base = (
        f"[turn tail] tool rounds used {used} / limit {limit} "
        f"(remaining {left})."
    )
    # Nudge once we enter the last third of the budget. For limit=15 this
    # fires from turn 10 onwards (matching the original constant).
    nudge_from = (2 * limit) // 3 + 1
    if used + 1 >= nudge_from:
        base += (
            " You are close to the budget limit. Unless one or two key pieces "
            "of evidence are missing, give the final answer from the material "
            "already collected; do not call more tools."
        )
    return base


async def _persist_tool_call(
    *,
    conversation_id: str,
    name: str,
    arguments: Any,
    result: Any,
    error: str | None,
    duration_ms: int,
) -> None:
    """Persist one tool_call row in its own transaction. Used by all four
    dispatch paths (unknown / exception / success / dedup-skipped)."""
    async with session_scope() as db:
        await session_service.append_tool_call(
            db,
            conversation_id=conversation_id,
            name=name,
            arguments=arguments,
            result=result,
            error=error,
            duration_ms=duration_ms,
        )
        await db.commit()


async def _dispatch_tool_calls(
    *,
    tool_calls,
    ctx: ToolContext,
    conversation_id: str,
    result_blocks: list[ToolResultBlock],
    guard: _CallGuard,
) -> AsyncIterator[AgentEvent]:
    """Preflight + parallel execution + completion-order drain.

    Async generator yielding AgentEvent (`tool_call`, `tool_result`,
    sometimes `user_artifact`). Two invariants worth pinning:

      - SSE events fire in *completion* order — users see fast tools
        finish first regardless of where they were in the assistant
        message. Each event carries `tool_call_id` so the frontend can
        pair the result back to the right step.
      - `result_blocks` is appended in *source* order so Anthropic's
        tool_use_id ↔ tool_result_id pairing stays valid when this
        message is fed back to the model.

    Guards (append-only — never edit prior history):
      - dedup-prior: (name, args) seen earlier this turn → synthesize a
        ToolResultBlock with the prior result_text, skip handler.
      - dedup-batch: (name, args) appears twice in *this* batch → only
        the first runs; the duplicate waits for the leader's result and
        reuses it. Saves real work when the model fans out the same
        read twice in one assistant message.
      - doom-loop: if the same key crossed DOOM_LOOP_THRESHOLD in the
        last DOOM_LOOP_WINDOW dispatched calls, append a STOP nudge to
        the last ToolResultBlock of *this* tool message.
    """
    n = len(tool_calls)
    placeholders: list[ToolResultBlock | None] = [None] * n
    keys: list[str] = []
    statuses: list[str] = []  # runnable | dup_prior | dup_batch | unknown
    leader_followers: dict[int, list[int]] = {}
    seen_in_batch: dict[str, int] = {}
    nudge_pending = False

    # ---- preflight: classify in source order, yield tool_call events ----
    for idx, tc in enumerate(tool_calls):
        # Pre-resolve every id referenced in args so the display
        # one-liner can show names instead of raw uuids. One DB round
        # trip per tool call, skipped when no ids of that kind appear.
        eids = tool_display.collect_entry_ids(tc.name, tc.arguments)
        tids = tool_display.collect_tag_ids(tc.name, tc.arguments)
        fids = tool_display.collect_folder_ids(tc.name, tc.arguments)
        cids = tool_display.collect_catalog_ids(tc.name, tc.arguments)
        name_by_id: dict[str, str] = {}
        tag_name_by_id: dict[str, str] = {}
        folder_name_by_id: dict[str, str] = {}
        catalog_name_by_id: dict[str, str] = {}
        if eids or tids or fids or cids:
            try:
                async with session_scope() as _db:
                    if eids:
                        # Agent may pass either a full uuid or a short
                        # hex prefix (>= 8 chars). Resolve each first so
                        # the display layer can label both forms with
                        # the entry's display_name.
                        full_by_raw: dict[str, str] = {}
                        for raw in set(eids):
                            full, err = await entries_repo.resolve_entry_id_prefix(
                                _db, raw,
                            )
                            if err is None:
                                full_by_raw[raw] = full
                        if full_by_raw:
                            rows = await entries_repo.list_live_with_file_by_ids(
                                _db, list(set(full_by_raw.values())),
                            )
                            full_to_name = {
                                entry.id: entry.display_name for entry, _ in rows
                            }
                            # Key the resolver by what the agent actually
                            # passed (raw), so format_tool_call can look
                            # up the same id it sees in `args`.
                            name_by_id = {
                                raw: full_to_name[full]
                                for raw, full in full_by_raw.items()
                                if full in full_to_name
                            }
                    if tids:
                        tag_name_by_id = await tags_repo.name_by_ids(
                            _db, list(set(tids))
                        )
                    if fids:
                        folder_name_by_id = await folders_repo.name_by_ids(
                            _db, list(set(fids))
                        )
                    if cids:
                        catalog_name_by_id = await catalogs_repo.name_by_ids(
                            _db, list(set(cids))
                        )
            except Exception:
                log.exception("tool_call display: name lookup failed")

        display = tool_display.format_tool_call(
            tc.name, tc.arguments,
            resolver=name_by_id.get,
            tag_resolver=tag_name_by_id.get,
            folder_resolver=folder_name_by_id.get,
            catalog_resolver=catalog_name_by_id.get,
        )

        yield AgentEvent(
            event_type="tool_call",
            data=json.dumps({
                "tool_call_id": tc.id,
                "name": tc.name,
                "arguments": tc.arguments,
                "display": display,
                "entry_names": name_by_id,
                "tag_names": tag_name_by_id,
                "folder_names": folder_name_by_id,
                "catalog_names": catalog_name_by_id,
            }, ensure_ascii=False),
        )

        key = guard.key(tc.name, tc.arguments)
        keys.append(key)

        if guard.should_nudge(key):
            nudge_pending = True
            guard.nudged = True

        if guard.is_duplicate(key):
            statuses.append("dup_prior")
            guard.recent.append(key)
            continue
        if key in seen_in_batch:
            statuses.append("dup_batch")
            leader_followers.setdefault(seen_in_batch[key], []).append(idx)
            guard.recent.append(key)
            continue
        if get_tool(tc.name) is None:
            statuses.append("unknown")
            continue
        seen_in_batch[key] = idx
        statuses.append("runnable")

    # ---- synchronous resolution: dup_prior + unknown ----
    for idx, tc in enumerate(tool_calls):
        s = statuses[idx]
        key = keys[idx]
        if s == "dup_prior":
            prior = guard.seen[key]
            prior_preview = guard.seen_previews.get(key) or "(see prior call)"
            placeholders[idx] = ToolResultBlock(
                tool_call_id=tc.id,
                content=(
                    "[runtime guard] duplicate call this turn — reusing "
                    f"prior result.\n{prior}"
                ),
            )
            yield AgentEvent(
                event_type="tool_result",
                data=json.dumps({
                    "tool_call_id": tc.id,
                    "name": tc.name, "ok": True, "deduped": True,
                    "preview": prior_preview[:TOOL_RESULT_PREVIEW_LEN],
                }, ensure_ascii=False),
            )
        elif s == "unknown":
            err = f"unknown tool: {tc.name}"
            await _persist_tool_call(
                conversation_id=conversation_id,
                name=tc.name, arguments=tc.arguments,
                result=None, error=err, duration_ms=0,
            )
            placeholders[idx] = ToolResultBlock(
                tool_call_id=tc.id,
                content=f"ERROR: {err}",
                is_error=True,
            )
            guard.remember(key, f"ERROR: {err}")
            yield AgentEvent(
                event_type="tool_result",
                data=json.dumps({
                    "tool_call_id": tc.id,
                    "name": tc.name, "ok": False, "error": err,
                }, ensure_ascii=False),
            )

    # ---- spawn runnables (each task owns its own DB session) ----
    tasks: dict[asyncio.Task, int] = {}
    for idx, tc in enumerate(tool_calls):
        if statuses[idx] != "runnable":
            continue
        reg = get_tool(tc.name)
        tasks[asyncio.create_task(_run_tool(reg, ctx, tc))] = idx

    # ---- drain in completion order ----
    try:
        while tasks:
            done, _pending = await asyncio.wait(
                list(tasks.keys()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                idx = tasks.pop(task)
                tc = tool_calls[idx]
                key = keys[idx]
                duration_ms, result, exc = task.result()
                if exc is not None:
                    log.exception("tool %s failed", tc.name, exc_info=exc)
                    err = repr(exc)
                    await _persist_tool_call(
                        conversation_id=conversation_id,
                        name=tc.name, arguments=tc.arguments,
                        result=None, error=err, duration_ms=duration_ms,
                    )
                    placeholders[idx] = ToolResultBlock(
                        tool_call_id=tc.id,
                        content=f"ERROR: {err}",
                        is_error=True,
                    )
                    guard.remember(key, f"ERROR: {err}")
                    yield AgentEvent(
                        event_type="tool_result",
                        data=json.dumps({
                            "tool_call_id": tc.id,
                            "name": tc.name, "ok": False, "error": err,
                            "duration_ms": duration_ms,
                        }, ensure_ascii=False),
                    )
                    # Fan-out failures to batch followers too — they share
                    # the leader's verdict.
                    for fidx in leader_followers.get(idx, ()):
                        ftc = tool_calls[fidx]
                        placeholders[fidx] = ToolResultBlock(
                            tool_call_id=ftc.id,
                            content=(
                                "[runtime guard] duplicate call this batch — "
                                f"leader failed.\nERROR: {err}"
                            ),
                            is_error=True,
                        )
                        yield AgentEvent(
                            event_type="tool_result",
                            data=json.dumps({
                                "tool_call_id": ftc.id,
                                "name": ftc.name, "ok": False,
                                "deduped": True, "error": err,
                            }, ensure_ascii=False),
                        )
                    continue

                # Side-channel: tools may attach `__user_only__` payload
                # shown to the UI but kept OUT of the model's tool_result
                # content. We persist the full result on the conversation
                # row so /info and replays still show it.
                user_only = None
                if isinstance(result, dict) and "__user_only__" in result:
                    user_only = result.get("__user_only__")
                    result_for_model_source = {
                        k: v for k, v in result.items() if k != "__user_only__"
                    }
                else:
                    result_for_model_source = result
                result_for_model = _copy_jsonish(result_for_model_source)
                if isinstance(result_for_model, (dict, list)):
                    result_text, _trim_marker = _structured_truncate(
                        result_for_model, MAX_TOOL_RESULT_LEN,
                    )
                else:
                    result_text = json.dumps(result_for_model, ensure_ascii=False)
                    if len(result_text) > MAX_TOOL_RESULT_LEN:
                        result_text = result_text[:MAX_TOOL_RESULT_LEN] + "...(truncated)"
                await _persist_tool_call(
                    conversation_id=conversation_id,
                    name=tc.name, arguments=tc.arguments,
                    result=result, error=None,
                    duration_ms=duration_ms,
                )
                if user_only is not None:
                    yield AgentEvent(
                        event_type="user_artifact",
                        data=json.dumps({
                            "tool_call_id": tc.id,
                            "tool": tc.name,
                            "payload": user_only,
                        }, ensure_ascii=False),
                    )
                preview = tool_display.format_tool_result_preview(
                    tc.name, result_for_model_source,
                )
                if len(preview) > TOOL_RESULT_PREVIEW_LEN:
                    preview = preview[:TOOL_RESULT_PREVIEW_LEN] + "..."
                placeholders[idx] = ToolResultBlock(
                    tool_call_id=tc.id, content=result_text,
                )
                guard.remember(key, result_text, preview=preview)
                yield AgentEvent(
                    event_type="tool_result",
                    data=json.dumps({
                        "tool_call_id": tc.id,
                        "name": tc.name, "ok": True, "preview": preview,
                        "duration_ms": duration_ms,
                    }, ensure_ascii=False),
                )
                # Fan-out the leader's result to its batch followers.
                for fidx in leader_followers.get(idx, ()):
                    ftc = tool_calls[fidx]
                    placeholders[fidx] = ToolResultBlock(
                        tool_call_id=ftc.id,
                        content=(
                            "[runtime guard] duplicate call this batch — "
                            f"reusing leader's result.\n{result_text}"
                        ),
                    )
                    yield AgentEvent(
                        event_type="tool_result",
                        data=json.dumps({
                            "tool_call_id": ftc.id,
                            "name": ftc.name, "ok": True,
                            "deduped": True,
                            "preview": preview[:TOOL_RESULT_PREVIEW_LEN],
                        }, ensure_ascii=False),
                    )
    finally:
        for t in tasks:
            t.cancel()

    # ---- finalize: source-order result_blocks + doom-loop nudge ----
    for ph in placeholders:
        if ph is not None:
            result_blocks.append(ph)

    if nudge_pending and result_blocks:
        # Decorate the last real tool_result with the STOP nudge. We
        # cannot append a synthetic ToolResultBlock with a fake
        # tool_use_id — Anthropic validates ids against prior tool_use
        # blocks and rejects unknown ones. Appending text to an existing
        # block's `content` keeps the message valid AND append-only at
        # the conversation level (we are decorating a block we just
        # created in this turn — never touching history).
        last = result_blocks[-1]
        result_blocks[-1] = ToolResultBlock(
            tool_call_id=last.tool_call_id,
            content=f"{last.content}\n\n{DOOM_LOOP_NUDGE}",
            is_error=last.is_error,
        )


async def _run_tool(reg, ctx: ToolContext, tc) -> tuple[int, Any, Exception | None]:
    """Execute one tool inside its own session_scope. Returns
    (duration_ms, result, exception). Never raises — failures travel
    back as the third tuple element so the dispatcher loop stays clean.
    """
    started = time.monotonic()
    try:
        async with session_scope() as db:
            result = await reg.handler(db, ctx, tc.arguments)
            await db.commit()
        return int((time.monotonic() - started) * 1000), result, None
    except Exception as exc:  # noqa: BLE001
        return int((time.monotonic() - started) * 1000), None, exc
