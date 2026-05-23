"""Agent runtime — design.md §10.2 + §12.2.

Plan-Execute loop. One `run_turn(session_id, user_message)` invocation:

  1. Open one conversation row (turn_index = next).
  2. Plan phase: ONE LLM call with `tools=[]`. Output is plan text only;
     stored in conversations.llm_calls under phase='plan'.
  3. Execute phase: up to MAX_EXECUTE_TURNS = 15 LLM calls with full tool
     binding. Each turn:
         - LLM call (records usage and llm_calls entry phase='execute')
         - if model returned tool_calls: dispatch them, append tool_calls
           records, feed results back as a `tool` message
         - if model returned text + no tool_calls AND stop_reason='end_turn':
           that's the final answer
     Starting at turn 11 (>= EXECUTE_NUDGE_FROM), the budget tail message
     pushes the agent to wrap up: "you have N rounds left, finalize soon".
  4. Truncation: if MAX_EXECUTE_TURNS hit without natural termination,
     synthesise a "I ran out of budget — partial answer follows" reply
     using whatever the model last produced; record outcome='deferred'.
  5. Finalize: write agent_response, ended_at; enqueue reflect_turn task
     (priority 30); record task_outcome.

Concurrency: this runtime assumes one in-flight turn per session. The API
layer should serialise per-session turns or the conversation rows will
race.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.stable_context import (
    build_stable_snapshot,
    render_system_prompt,
)
from marginalia.agent.tools import ToolContext, all_tool_defs, get_tool
from marginalia.agent.types import AgentTurnError, TurnResult, TurnUsage
from marginalia.db.models import Conversation
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    get_chat_client,
)
from marginalia.services import sessions as session_service
from marginalia.services.task_outcomes import (
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_REFLECT_TURN

log = logging.getLogger(__name__)

MAX_EXECUTE_TURNS = 15
EXECUTE_NUDGE_FROM = 11    # from this turn onwards, append wrap-up nudge
MAX_TOOL_RESULT_LEN = 50_000  # tool result truncation guard
PLAN_MAX_TOKENS = 1024
EXECUTE_MAX_TOKENS = 2048


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def run_turn(
    *,
    session_id: str,
    user_message: str,
) -> TurnResult:
    """Run one user turn. Returns TurnResult with the final answer."""
    if not user_message.strip():
        raise AgentTurnError("user_message is empty")

    # ---- 1. open conversation -------------------------------------------
    async with session_scope() as db:
        # determine next turn_index
        last = (
            await db.execute(
                select(Conversation.turn_index)
                .where(Conversation.session_id == session_id)
                .order_by(Conversation.turn_index.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        turn_index = (last or -1) + 1

        conv = await session_service.start_conversation(
            db, session_id=session_id, turn_index=turn_index,
            user_message=user_message,
        )
        snapshot = await build_stable_snapshot(db)
        await db.commit()
        conversation_id = conv.id

    system_prompt = render_system_prompt(snapshot)
    chat = get_chat_client("chat")

    # ---- 2. plan phase --------------------------------------------------
    plan_text = await _run_plan_phase(
        chat=chat,
        system_prompt=system_prompt,
        user_message=user_message,
        conversation_id=conversation_id,
    )

    # ---- 3. execute phase ----------------------------------------------
    final_answer, truncated = await _run_execute_phase(
        chat=chat,
        system_prompt=system_prompt,
        plan_text=plan_text,
        user_message=user_message,
        conversation_id=conversation_id,
        session_id=session_id,
    )

    # ---- 4. finalize ---------------------------------------------------
    async with session_scope() as db:
        await session_service.finalize_conversation(
            db,
            conversation_id=conversation_id,
            agent_response=final_answer,
        )
        # enqueue reflect_turn (priority 30)
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
            outcome="deferred" if truncated else "applied",
            detail={
                "turn_index": turn_index,
                "session_id": session_id,
                "truncated": truncated,
            },
        )
        # accumulate usage from the freshly-flushed conversation
        conv = await db.get(Conversation, conversation_id)
        usage = TurnUsage(
            input_tokens=conv.total_input_tokens or 0,
            output_tokens=conv.total_output_tokens or 0,
            tool_calls=conv.total_tool_calls or 0,
            llm_calls=conv.total_llm_calls or 0,
            duration_ms=conv.total_duration_ms or 0,
            cost_estimate=conv.total_cost_estimate or Decimal("0"),
        )
        await db.commit()

    return TurnResult(
        session_id=session_id,
        conversation_id=conversation_id,
        agent_response=final_answer,
        plan_text=plan_text,
        usage=usage,
        truncated=truncated,
    )


# ---- plan -----------------------------------------------------------------

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
        max_tokens=PLAN_MAX_TOKENS,
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


# ---- execute --------------------------------------------------------------

async def _run_execute_phase(
    *,
    chat,
    system_prompt: str,
    plan_text: str,
    user_message: str,
    conversation_id: str,
    session_id: str,
) -> tuple[str, bool]:
    """Execute loop. Returns (final_answer_text, truncated)."""
    tool_defs = all_tool_defs()
    ctx = ToolContext(session_id=session_id, conversation_id=conversation_id)

    # Maintain the turn-by-turn message list. We seed with the original user
    # question + the plan as an assistant turn so the model can reference it.
    messages: list[ChatMessage] = [
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

        started = time.monotonic()
        resp = await chat.complete(ChatRequest(
            system=system_prompt,
            messages=loop_messages,
            max_tokens=EXECUTE_MAX_TOKENS,
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

            tool_result_blocks = await _dispatch_tool_calls(
                tool_calls=resp.tool_calls,
                ctx=ctx,
                conversation_id=conversation_id,
            )
            messages.append(ChatMessage(role="tool", content=tool_result_blocks))
            last_text = resp.text or last_text
            continue

        # No tool calls: this is the final assistant message
        last_text = resp.text or last_text
        if resp.stop_reason in ("end_turn", "stop_sequence"):
            return (resp.text or last_text or "(无回答)", False)
        if resp.stop_reason == "max_tokens":
            log.warning("execute turn %d hit max_tokens; treating as final", turn)
            return (resp.text or last_text or "(无回答)", False)

    log.warning("conversation %s hit MAX_EXECUTE_TURNS=%d", conversation_id,
                MAX_EXECUTE_TURNS)
    fallback = (
        last_text
        or "对不起——本轮调查超过了预算上限，没能给出完整回答。请把问题分小或换个角度再试。"
    )
    return (fallback, True)


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


async def _dispatch_tool_calls(
    *,
    tool_calls,
    ctx: ToolContext,
    conversation_id: str,
) -> list[ToolResultBlock]:
    """Run each tool inside its own session_scope; record on conversation."""
    out: list[ToolResultBlock] = []
    for tc in tool_calls:
        reg = get_tool(tc.name)
        started = time.monotonic()
        if reg is None:
            err = f"unknown tool: {tc.name}"
            duration_ms = int((time.monotonic() - started) * 1000)
            async with session_scope() as db:
                await session_service.append_tool_call(
                    db,
                    conversation_id=conversation_id,
                    name=tc.name,
                    arguments=tc.arguments,
                    result=None,
                    error=err,
                    duration_ms=duration_ms,
                )
                await db.commit()
            out.append(ToolResultBlock(
                tool_call_id=tc.id,
                content=f"ERROR: {err}",
                is_error=True,
            ))
            continue

        try:
            async with session_scope() as db:
                result = await reg.handler(db, ctx, tc.arguments)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - tool errors are reported, not raised
            log.exception("tool %s failed", tc.name)
            duration_ms = int((time.monotonic() - started) * 1000)
            async with session_scope() as db:
                await session_service.append_tool_call(
                    db,
                    conversation_id=conversation_id,
                    name=tc.name,
                    arguments=tc.arguments,
                    result=None,
                    error=repr(exc),
                    duration_ms=duration_ms,
                )
                await db.commit()
            out.append(ToolResultBlock(
                tool_call_id=tc.id,
                content=f"ERROR: {exc!r}",
                is_error=True,
            ))
            continue

        duration_ms = int((time.monotonic() - started) * 1000)
        # serialise the result for the model + storage
        import json
        result_text = json.dumps(result, ensure_ascii=False)
        if len(result_text) > MAX_TOOL_RESULT_LEN:
            result_text = result_text[:MAX_TOOL_RESULT_LEN] + "...(truncated)"
        async with session_scope() as db:
            await session_service.append_tool_call(
                db,
                conversation_id=conversation_id,
                name=tc.name,
                arguments=tc.arguments,
                result=result,
                duration_ms=duration_ms,
            )
            await db.commit()
        out.append(ToolResultBlock(
            tool_call_id=tc.id,
            content=result_text,
        ))
    return out
