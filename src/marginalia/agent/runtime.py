"""Agent runtime — DESIGN.md §10.2 + §12.2.

Plan-Execute loop, exposed as async generator yielding AgentEvent frames
for SSE streaming. One `run_turn(session_id, user_message)` invocation:

  1. Open one conversation row (turn_index = next). Yield "conversation".
  2. Plan phase: yield "planning", do ONE LLM call with `tools=[]`,
     yield "plan" with full plan_text. Stored in conversations.llm_calls
     under phase='plan'. If plan_text starts with `NO_PLAN:` the trailing
     answer is treated as the final answer and execute is skipped.
  3. Execute phase: up to MAX_EXECUTE_TURNS = 15 LLM calls. For each:
         - yield "thinking", LLM call (records usage)
         - if model returned tool_calls: yield "tool_call" per call,
           dispatch (with dedup + doom-loop guards), yield "tool_result",
           feed back as `tool` message
         - if model returned text + no tool_calls AND stop_reason='end_turn':
           yield "answer" with final text
     Starting at turn 11 (>= EXECUTE_NUDGE_FROM), append wrap-up tail.
  4. Truncation: if MAX_EXECUTE_TURNS hit, yield "answer" with fallback
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

Concurrency: this runtime assumes one in-flight turn per session. The API
layer should serialise per-session turns or the conversation rows will
race.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, AsyncIterator

from marginalia.agent.stable_context import (
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

MAX_EXECUTE_TURNS = 15
EXECUTE_NUDGE_FROM = 11
MAX_TOOL_RESULT_LEN = 50_000
# Default token budgets — overridable per-deploy via AGENT_PLAN_MAX_TOKENS /
# AGENT_EXECUTE_MAX_TOKENS in settings. Sized for gpt-4o-class models; bump
# for long-context backends (DeepSeek-V3, Claude 3.5 Sonnet, etc.).
PLAN_MAX_TOKENS = 1024
EXECUTE_MAX_TOKENS = 2048
TOOL_RESULT_PREVIEW_LEN = 240

NO_PLAN_PREFIX = "NO_PLAN:"

# Doom-loop: if the same (name, canonical_args) shows up
# DOOM_LOOP_THRESHOLD times within the last DOOM_LOOP_WINDOW tool calls,
# inject a STOP nudge. The threshold is one above the dedup floor — dedup
# already neutralises duplicate work, so this fires only on near-duplicate
# patterns the model is iterating on (slightly different args each time).
DOOM_LOOP_WINDOW = 6
DOOM_LOOP_THRESHOLD = 3
DOOM_LOOP_NUDGE = (
    "[runtime guard] 你最近反复在用相似参数调用同一个工具，可能陷入循环。"
    "请停止扩展工具调用，基于已有结果直接给出最终回答。"
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
        turn_index = (last or -1) + 1

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
    yield AgentEvent(event_type="plan", data=plan_text)

    outcome = _ExecuteOutcome()
    no_plan_answer = _extract_no_plan_answer(plan_text)
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
        resumed_history = await _build_resumed_messages(
            session_id, current_conversation_id=conversation_id,
        )
        async for ev in _run_execute_phase(
            chat=chat,
            system_prompt=execute_system,
            plan_text=plan_text,
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
        }),
    )


# ---- plan -----------------------------------------------------------------

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


RESUME_BOUNDARY_NOTE = (
    "（以上为本会话之前已完成的回合回放；接下来的 user 消息是新一轮真实输入，"
    "请基于完整对话上下文继续调查与作答。）"
)


async def _build_resumed_messages(
    session_id: str, *, current_conversation_id: str,
) -> list[ChatMessage]:
    """Reconstruct the LLM's prior conversation history for an open session.

    Replays every prior turn — user message, every tool_call/tool_result
    pair, and the final agent_response — so the executor sees the same
    context it would have during the original turns. Synthesizes fresh
    `tool_use_id`s per resumed turn (`tu_resume_<turn>_<idx>`); the model
    only needs ToolUse↔ToolResult ids to be self-consistent within one
    request, not stable across turns.

    Closes with a Chinese boundary note (system-note in user-role since
    the top-level `system` field is already pinned) so the model can
    distinguish replayed history from the live new turn.

    Cost: full backfill is the most token-expensive option but the most
    faithful — agent sees every tool call it ran. If sessions grow long
    we can swap this for a Q+A-only or sliding-window flavour later.
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
                if len(body) > MAX_TOOL_RESULT_LEN:
                    body = body[:MAX_TOOL_RESULT_LEN] + "\n…[truncated on resume]"
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

# Same shape as services/exports.py:_FOOTNOTE_RE — agent emits citation
# defs as `[^a]: entry_id=<uuid>[, section_id=<sid>] - reason`. For the
# live SSE answer we resolve the uuid to display_name and rewrite to a
# user-friendly form. The persisted `agent_response` (and therefore
# downstream exports) keep the raw form so the export parser still works.
#
# UUID is matched strictly so the regex doesn't greedy-backtrack and eat
# the trailing `, section_id=...` / parenthetical / em-dash + reason.
# Models routinely wrap the uuid (and section_id value) in backticks
# because they treat ids as inline code — `\`?` makes those backticks
# optional so the rewrite still fires.
_LIVE_FOOTNOTE_RE = re.compile(
    r"^\[\^([^\]]+)\]:\s*entry_id\s*=\s*`?"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"`?"
    r"(?:\s*,\s*section_id\s*=\s*`?([^\s,`]+)`?)?"
    r"(?:\s+\([^)]*\))?"
    r"(?:\s*[-—–]\s*(.+?))?"
    r"\s*$",
    re.MULTILINE,
)


async def _rewrite_footnotes_for_display(answer: str) -> str:
    """Resolve `[^a]: entry_id=<uuid>...` defs to `[^a]: [name](entry:<short>)...`.

    Same logic as services/exports.py:render_inline_markdown but for live
    streaming — looks up display_name in one DB round trip and rewrites
    each definition. Missing entries fall back to `(entry <short> unavailable)`.
    Body `[^a]` markers are untouched so GFM footnote linking still works.
    """
    if not answer or "entry_id" not in answer:
        return answer
    matches = list(_LIVE_FOOTNOTE_RE.finditer(answer))
    if not matches:
        return answer

    entry_ids = list({m.group(2).strip() for m in matches})
    name_by_id: dict[str, str] = {}
    try:
        async with session_scope() as db:
            rows = await entries_repo.list_live_with_file_by_ids(db, entry_ids)
            name_by_id = {entry.id: entry.display_name for entry, _ in rows}
    except Exception:
        log.exception("footnote rewrite: entry lookup failed; keeping raw form")
        return answer

    def _replace(m: re.Match[str]) -> str:
        marker = m.group(1)
        eid = m.group(2).strip()
        # section_id (m.group(3)) is intentionally not surfaced in the
        # user-visible footnote — it's an LLM-internal handle (s1, s2, …)
        # that means nothing to a reader. The persisted footnote form
        # still carries it for export/tool dispatch.
        reason = m.group(4).strip() if m.group(4) else None
        short = eid[:8]
        name = name_by_id.get(eid)
        if name is None:
            head = f"(entry {short} unavailable)"
        else:
            head = f"[{name}](entry:{short})"
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
    built by `_build_resumed_messages` and is prepended ahead of the
    current turn's user message, with a boundary note baked in by the
    builder.
    """
    tool_defs = all_tool_defs()
    ctx = ToolContext(session_id=session_id, conversation_id=conversation_id)
    guard = _CallGuard()

    messages: list[ChatMessage] = list(resumed_history or []) + [
        ChatMessage(role="user", content=user_message),
        ChatMessage(role="assistant", content=(
            "已制定计划：\n" + (plan_text or "(无具体计划，直接基于问题回答)")
        )),
    ]

    last_text: str | None = None
    for turn in range(MAX_EXECUTE_TURNS):
        budget_tail = _budget_tail(turn=turn)
        loop_messages = messages + [
            ChatMessage(role="user", content=budget_tail)
        ] if budget_tail else messages

        yield AgentEvent(event_type="thinking")

        started = time.monotonic()
        resp = await chat.complete(ChatRequest(
            system=system_prompt,
            messages=loop_messages,
            max_tokens=get_settings().agent_execute_max_tokens,
            tools=tool_defs,
            tool_choice="auto",
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
                extra={"execute_turn": turn, "stop_reason": resp.stop_reason},
            )
            await db.commit()

        if resp.tool_calls:
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

        last_text = resp.text or last_text
        if resp.stop_reason in ("end_turn", "stop_sequence"):
            answer = _strip_leaked_no_plan(resp.text or last_text or "(无回答)")
            outcome.answer = answer
            yield AgentEvent(
                event_type="answer",
                data=await _rewrite_footnotes_for_display(answer),
            )
            return
        if resp.stop_reason == "max_tokens":
            log.warning("execute turn %d hit max_tokens; treating as final", turn)
            answer = _strip_leaked_no_plan(resp.text or last_text or "(无回答)")
            outcome.answer = answer
            yield AgentEvent(
                event_type="answer",
                data=await _rewrite_footnotes_for_display(answer),
            )
            return

    log.warning("conversation %s hit MAX_EXECUTE_TURNS=%d", conversation_id,
                MAX_EXECUTE_TURNS)
    fallback = _strip_leaked_no_plan(
        last_text
        or "对不起——本轮调查超过了预算上限，没能给出完整回答。请把问题分小或换个角度再试。"
    )
    outcome.truncated = True
    outcome.answer = fallback
    yield AgentEvent(
        event_type="answer",
        data=await _rewrite_footnotes_for_display(fallback),
    )


def _budget_tail(*, turn: int) -> str | None:
    """Return the budget tail message for execute turn `turn` (0-indexed).

    Always show 'rounds used / left'. From EXECUTE_NUDGE_FROM onwards add a
    wrap-up nudge so the agent stops gathering and writes the answer.
    """
    used = turn  # turns already consumed before this call
    left = MAX_EXECUTE_TURNS - used
    base = f"[turn tail] 已用工具回合 {used} / 上限 {MAX_EXECUTE_TURNS}（剩余 {left}）。"
    if used + 1 >= EXECUTE_NUDGE_FROM:
        base += (
            " 你已接近预算上限——除非缺一两个关键证据，本轮请直接给出"
            "基于已收集材料的最终回答；不要再调用工具。"
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


def _emit_failure(
    *,
    tc,
    error: str,
    result_blocks: list[ToolResultBlock],
    guard: _CallGuard,
    key: tuple[str, str],
) -> AgentEvent:
    """Build the failure ToolResultBlock + remember it for dedup, return the
    AgentEvent the caller should yield. Centralizes the `unknown tool` and
    `exception during handler` paths."""
    result_blocks.append(ToolResultBlock(
        tool_call_id=tc.id,
        content=f"ERROR: {error}",
        is_error=True,
    ))
    guard.remember(key, f"ERROR: {error}")
    return AgentEvent(
        event_type="tool_result",
        data=json.dumps({
            "name": tc.name, "ok": False, "error": error,
        }, ensure_ascii=False),
    )


async def _dispatch_tool_calls(
    *,
    tool_calls,
    ctx: ToolContext,
    conversation_id: str,
    result_blocks: list[ToolResultBlock],
    guard: _CallGuard,
) -> AsyncIterator[AgentEvent]:
    """Run each tool inside its own session_scope; record on conversation.

    Async generator yielding AgentEvent (`tool_call`, `tool_result`).
    Per-call ToolResultBlocks are appended to `result_blocks` so the
    caller can feed them back to the model in a single tool message —
    avoids an interleaved `AgentEvent | ToolResultBlock` stream the
    caller would have to isinstance-filter.

    Guards (append-only — never edit prior history):
      - dedup: if (name, args) already ran this turn, synthesize a
        ToolResultBlock with the prior result_text, skip handler.
      - doom-loop: if the same key crossed DOOM_LOOP_THRESHOLD in the
        last DOOM_LOOP_WINDOW dispatched calls, append a STOP nudge
        ToolResultBlock to *this* tool message.
    """
    nudge_pending = False
    for tc in tool_calls:
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
                        rows = await entries_repo.list_live_with_file_by_ids(
                            _db, list(set(eids))
                        )
                        name_by_id = {entry.id: entry.display_name for entry, _ in rows}
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

        if guard.should_nudge(key):
            nudge_pending = True
            guard.nudged = True

        if guard.is_duplicate(key):
            prior = guard.seen[key]
            prior_preview = guard.seen_previews.get(key) or "(see prior call)"
            guard.recent.append(key)  # record the attempt for doom-loop
            yield AgentEvent(
                event_type="tool_result",
                data=json.dumps({
                    "name": tc.name, "ok": True, "deduped": True,
                    "preview": prior_preview[:TOOL_RESULT_PREVIEW_LEN],
                }, ensure_ascii=False),
            )
            result_blocks.append(ToolResultBlock(
                tool_call_id=tc.id,
                content=(
                    "[runtime guard] duplicate call this turn — reusing "
                    f"prior result.\n{prior}"
                ),
            ))
            continue

        reg = get_tool(tc.name)
        started = time.monotonic()
        if reg is None:
            err = f"unknown tool: {tc.name}"
            await _persist_tool_call(
                conversation_id=conversation_id,
                name=tc.name, arguments=tc.arguments,
                result=None, error=err,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            yield _emit_failure(
                tc=tc, error=err,
                result_blocks=result_blocks, guard=guard, key=key,
            )
            continue

        try:
            async with session_scope() as db:
                result = await reg.handler(db, ctx, tc.arguments)
                await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", tc.name)
            await _persist_tool_call(
                conversation_id=conversation_id,
                name=tc.name, arguments=tc.arguments,
                result=None, error=repr(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            yield _emit_failure(
                tc=tc, error=repr(exc),
                result_blocks=result_blocks, guard=guard, key=key,
            )
            continue

        duration_ms = int((time.monotonic() - started) * 1000)
        # Side-channel: tools may attach a `__user_only__` payload that is
        # shown to the user (UI artifact, e.g. Vega-Lite spec) but kept
        # OUT of the model's tool_result content — the model gets only
        # the lightweight summary and the chart_id. We persist the full
        # result (incl. side-channel) on the conversation row so /info
        # and replays still show it.
        user_only = None
        if isinstance(result, dict) and "__user_only__" in result:
            user_only = result.get("__user_only__")
            result_for_model = {k: v for k, v in result.items() if k != "__user_only__"}
        else:
            result_for_model = result
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
                    "tool": tc.name,
                    "payload": user_only,
                }, ensure_ascii=False),
            )
        preview = tool_display.format_tool_result_preview(tc.name, result_for_model)
        if len(preview) > TOOL_RESULT_PREVIEW_LEN:
            preview = preview[:TOOL_RESULT_PREVIEW_LEN] + "..."
        yield AgentEvent(
            event_type="tool_result",
            data=json.dumps({
                "name": tc.name, "ok": True, "preview": preview,
            }, ensure_ascii=False),
        )
        result_blocks.append(ToolResultBlock(
            tool_call_id=tc.id,
            content=result_text,
        ))
        guard.remember(key, result_text, preview=preview)

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
