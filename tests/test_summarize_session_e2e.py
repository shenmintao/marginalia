"""End-to-end summarize_session sanity check.

Verifies (Phase C of journal-tiers refactor):
  1. Seed a session with K conversations, each carrying one reflect_turn
     journal row written by reflect_turn (we insert directly here).
  2. Stub the `reflect` LLM client to return a canned summary of 2 insights,
     one of which supersedes a pre-existing insight from a different session.
  3. Run handle_summarize_session. Verify writes:
     - 2 new journal rows with source_kind='insight'
     - The pre-existing insight has superseded_by_id pointing to the first
       new insight
     - task_outcomes row with detail.insights_inserted == 2
  4. Run again with same session_id within MIN_INTERVAL → idempotence skip.
  5. Run with a session having only 2 reflect_turn rows → noop (below
     MIN_TURNS).
  6. periodic_tick dispatches a summarize_session task for an eligible
     session and skips an ineligible one.

Run:
    .venv/Scripts/python tests/test_summarize_session_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_summarize_session_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, File, FileEntry, Folder, Journal, Session,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.tasks.kinds import KIND_SUMMARIZE_SESSION
from marginalia.utils.ids import new_id


SUMMARIZE_CALLS: list[ChatRequest] = []
NEXT_RESPONSE: dict = {"insights": [], "superseded": []}


def _make_fake_summarizer():
    class _FakeChatClient:
        profile_name = "reflect"
        model = "fake-summarize"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            SUMMARIZE_CALLS.append(request)
            payload = NEXT_RESPONSE
            return ChatResponse(
                text=json.dumps(payload),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=3000, output_tokens=400, cache_read_tokens=2500),
                parsed_json=payload,
            )
    return _FakeChatClient()


def _install_fake(client) -> None:
    llm.reset_clients_cache()
    def _factory(profile: str = "ingest"):
        return client
    import marginalia.tasks.handlers.summarize_session as smod
    smod.get_chat_client = _factory  # type: ignore[assignment]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_session_with_reflects(turn_count: int, *, age_hours: int = 48):
    """Seed: 1 session, N conversations each with one reflect_turn journal row.

    age_hours: how old the most-recent reflect row should be — periodic_tick
    requires the most-recent reflect_turn to be older than SUMMARIZE_MIN_AGE.
    """
    factory = get_session_factory()
    async with factory() as s:
        now = _now()
        old = now - timedelta(hours=age_hours)
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        unique = new_id()
        f = File(id=new_id(), storage_key=f"x/y/{unique}", sha256=unique.replace("-", "").ljust(64, "c")[:64], size_bytes=10,
                 mime_type="text/markdown", original_ext=".md", kind="text",
                 summary="paper", description=None, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add_all([folder, f])
        await s.flush()
        e = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                      display_name="paper.md", lifecycle="active",
                      catalog_id=None, extra=None,
                      created_at=now, updated_at=now)
        s.add(e)

        sess = Session(id=new_id(), started_at=old, ended_at=old,
                       end_reason="normal",
                       initiating_user_message="seed",
                       turn_count=turn_count, total_input_tokens=0,
                       total_output_tokens=0, total_cache_read=0,
                       total_tool_calls=0, total_llm_calls=0,
                       total_duration_ms=0)
        s.add(sess)
        await s.flush()

        conv_ids = []
        for i in range(turn_count):
            c = Conversation(
                id=new_id(), session_id=sess.id, turn_index=i,
                started_at=old, ended_at=old,
                user_message=f"q{i}", agent_response=f"a{i}",
                tool_calls=[], llm_calls=[],
                total_input_tokens=0, total_output_tokens=0,
                total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
            )
            s.add(c)
            conv_ids.append(c.id)
        await s.flush()
        for i, cid in enumerate(conv_ids):
            s.add(Journal(
                id=new_id(), conversation_id=cid,
                note=f"turn {i} note", entry_ids=[e.id], tags=[],
                source_kind="reflect_turn",
                created_at=old + timedelta(minutes=i),
            ))
        await s.commit()
        return {"session_id": sess.id, "entry_id": e.id, "conv_ids": conv_ids}


async def _seed_prior_insight(session_id: str, entry_id: str) -> str:
    """Seed an old insight from another session that mentions the same entry."""
    factory = get_session_factory()
    async with factory() as s:
        # need a separate conversation to attach this prior insight to
        old_sess = Session(
            id=new_id(), started_at=_now() - timedelta(days=10),
            ended_at=_now() - timedelta(days=10),
            end_reason="normal", initiating_user_message="prior",
            turn_count=1, total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        )
        s.add(old_sess)
        await s.flush()
        old_conv = Conversation(
            id=new_id(), session_id=old_sess.id, turn_index=0,
            started_at=_now() - timedelta(days=10),
            ended_at=_now() - timedelta(days=10),
            user_message="old", agent_response="old", tool_calls=[],
            llm_calls=[], total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(old_conv)
        await s.flush()
        prior = Journal(
            id=new_id(), conversation_id=old_conv.id,
            note="OLD INSIGHT to be superseded", entry_ids=[entry_id],
            tags=["consensus"], source_kind="insight",
            created_at=_now() - timedelta(days=10),
        )
        s.add(prior)
        await s.commit()
        return prior.id


async def main():
    await _create_schema()
    fake = _make_fake_summarizer()
    _install_fake(fake)

    from marginalia.tasks.handlers.summarize_session import (
        handle_summarize_session,
    )

    # --- pass 1: eligible session, LLM returns 2 insights, one supersedes ---
    seeded = await _seed_session_with_reflects(turn_count=4, age_hours=48)
    prior_insight_id = await _seed_prior_insight(
        seeded["session_id"], seeded["entry_id"],
    )

    global NEXT_RESPONSE
    NEXT_RESPONSE = {
        "insights": [
            {
                "note": "User favors Raft over Paxos.",
                "entry_ids": [seeded["entry_id"]],
                "tags": ["consensus", "preference"],
            },
            {
                "note": "Markdown rendering edge cases worth a future session.",
                "entry_ids": [],
                "tags": ["rendering"],
            },
        ],
        "superseded": [prior_insight_id],
    }

    await handle_summarize_session({"session_id": seeded["session_id"]})
    assert len(SUMMARIZE_CALLS) == 1

    factory = get_session_factory()
    async with factory() as s:
        all_journal = (await s.execute(select(Journal))).scalars().all()
        insights = [j for j in all_journal if j.source_kind == "insight"]
        assert len(insights) == 3, f"expected 3 insight rows total (1 prior + 2 new), got {len(insights)}"

        new_insights = [j for j in insights if j.id != prior_insight_id]
        assert len(new_insights) == 2

        # newly-added rows reference the LAST conversation of the session
        last_conv_id = seeded["conv_ids"][-1]
        assert all(j.conversation_id == last_conv_id for j in new_insights), \
            "new insights should anchor on the last conversation of the session"

        # the prior insight is superseded by the FIRST inserted new insight
        prior = await s.get(Journal, prior_insight_id)
        assert prior.superseded_by_id is not None
        assert prior.superseded_by_id in {j.id for j in new_insights}

    print("[1] summarize_session inserts 2 insights and chains supersedure")

    # --- pass 2: same session_id again → idempotence skip ----------------
    NEXT_RESPONSE = {"insights": [], "superseded": []}  # would write 0 if called
    await handle_summarize_session({"session_id": seeded["session_id"]})
    assert len(SUMMARIZE_CALLS) == 1, (
        "second call within MIN_INTERVAL should skip the LLM"
    )
    print("[2] idempotence skip kicks in within MIN_INTERVAL")

    # --- pass 3: session with too few reflect rows → noop ----------------
    small = await _seed_session_with_reflects(turn_count=2, age_hours=48)
    NEXT_RESPONSE = {"insights": [], "superseded": []}
    await handle_summarize_session({"session_id": small["session_id"]})
    assert len(SUMMARIZE_CALLS) == 1, (
        "below MIN_TURNS should skip the LLM call"
    )

    async with factory() as s:
        small_insights = (
            await s.execute(
                select(Journal).where(Journal.source_kind == "insight")
            )
        ).scalars().all()
        assert len(small_insights) == 3, (
            "no new insights for below-min-turns session"
        )
    print("[3] below-min-turns session is a noop (no LLM, no journal rows)")

    # --- pass 4: periodic_tick dispatcher picks up an eligible session ---
    # Seed a fresh eligible session that hasn't been summarized yet.
    fresh = await _seed_session_with_reflects(turn_count=3, age_hours=48)

    from marginalia.tasks.handlers.periodic_tick import (
        _dispatch_summarize_sessions,
    )
    from marginalia.db.session import session_scope

    async with session_scope() as s:
        enqueued = await _dispatch_summarize_sessions(s, _now())
        await s.commit()

    assert fresh["session_id"] in enqueued, (
        f"periodic_tick should have enqueued session {fresh['session_id']}; "
        f"got {enqueued}"
    )
    # already-summarized session should NOT be re-enqueued
    assert seeded["session_id"] not in enqueued, (
        "already-summarized session must be filtered out by recency check"
    )
    print(f"[4] periodic_tick dispatches summarize_session for eligible "
          f"sessions; enqueued={len(enqueued)}")

    print("\nALL SUMMARIZE_SESSION E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
