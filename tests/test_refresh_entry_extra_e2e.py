"""End-to-end refresh_entry_extra (Cycle 25).

Run:
    .venv/Scripts/python tests/test_refresh_entry_extra_e2e.py

Verifies:
  1. Eligibility: only entries with ≥ MIN_JOURNALS journal mentions in
     the WINDOW_DAYS window are candidates.
  2. lifecycle ∉ {active, manual_active} excludes an entry.
  3. soft-deleted entry excluded.
  4. Successful LLM rewrite → file_entries.extra UPDATEd, audit
     `entry_extra_refreshed` written, task_outcomes 'applied'.
  5. LLM returns same text as current_extra → no UPDATE, task_outcomes
     'noop' with reason='extra_unchanged'.
  6. dry_run → no UPDATE but task_outcomes records 'would_write'.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_refresh_entry_extra_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, EntryTag, File, FileEntry, Folder,
    Journal, Session, Tag,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.utils.ids import new_id


CALL_LOG: list[ChatRequest] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_fake(plan: dict[str, str]):
    """plan: entry_id → new_extra string the LLM should return."""

    class _Fake:
        profile_name = "ingest"
        model = "fake-ingest"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            CALL_LOG.append(request)
            ut = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
            ctx_start = ut.index("<context>") + len("<context>")
            ctx_end = ut.index("</context>")
            payload = json.loads(ut[ctx_start:ctx_end].strip())
            eid = payload["entry"]["entry_id"]
            new_extra = plan.get(eid, payload["entry"]["current_extra"])
            out = {"extra": new_extra}
            return ChatResponse(
                text=json.dumps(out),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=600, output_tokens=120,
                                 cache_read_tokens=400),
                parsed_json=out,
            )
    return _Fake()


def _install_fake(client):
    llm.reset_clients_cache()
    import marginalia.tasks.handlers.refresh_entry_extra as mod
    mod.get_chat_client = lambda profile="ingest": client  # type: ignore[assignment]


async def _seed():
    """Seed:
      e_a: lifecycle=active, 4 journal mentions in window → ELIGIBLE
      e_b: lifecycle=active, 3 journal mentions but only 1 within window → INELIGIBLE
      e_c: lifecycle=manual_active, 3 journal mentions → ELIGIBLE
      e_d: lifecycle=archived (auto), 5 journal mentions → INELIGIBLE
      e_e: lifecycle=active, 3 journal mentions, soft-deleted → INELIGIBLE
      e_f: lifecycle=active, 3 journal mentions, no extra change → noop case
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        # one shared file ok
        f = File(id=new_id(), storage_key="00/aa/x", sha256="z" * 64,
                 size_bytes=10, mime_type="text/plain", original_ext=".txt",
                 kind="text", summary="paper",
                 description={"sections": []}, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f); await s.flush()

        def _mk(name: str, lifecycle: str = "active",
                deleted: bool = False,
                extra: str = "old extra") -> FileEntry:
            e = FileEntry(
                id=new_id(), folder_id=folder.id, file_id=f.id,
                display_name=name, lifecycle=lifecycle,
                catalog_id=None, extra=extra,
                deleted_at=(_now() if deleted else None),
                created_at=now, updated_at=now,
            )
            s.add(e)
            return e

        e_a = _mk("a.md", "active", extra="A original extra")
        e_b = _mk("b.md", "active", extra="B original extra")
        e_c = _mk("c.md", "manual_active", extra="C original extra")
        e_d = _mk("d.md", "archived", extra="D original extra")
        e_e = _mk("e.md", "active", deleted=True, extra="E original extra")
        e_f = _mk("f.md", "active", extra="F unchanged extra")
        await s.flush()

        # session + conversation for journals' FK
        sess = Session(id=new_id(), started_at=now, ended_at=now,
                       end_reason="normal", initiating_user_message="x",
                       turn_count=0, total_input_tokens=0, total_output_tokens=0,
                       total_cache_read=0, total_tool_calls=0,
                       total_llm_calls=0, total_duration_ms=0)
        s.add(sess); await s.flush()
        conv = Conversation(id=new_id(), session_id=sess.id, turn_index=0,
                            started_at=now, ended_at=now,
                            user_message="x", agent_response="x",
                            tool_calls=[], llm_calls=[],
                            total_input_tokens=0, total_output_tokens=0,
                            total_tool_calls=0, total_llm_calls=0,
                            total_duration_ms=0)
        s.add(conv); await s.flush()

        # Journals — recent (within window) and old
        recent = now - timedelta(days=2)
        ancient = now - timedelta(days=60)
        # e_a: 4 recent journals
        for i in range(4):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note=f"a journal {i}", entry_ids=[e_a.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=recent))
        # e_b: 1 recent + 2 ancient → only 1 within window → INELIGIBLE
        s.add(Journal(id=new_id(), conversation_id=conv.id,
                      note="b recent", entry_ids=[e_b.id],
                      tags=[], source_kind="reflect_turn",
                      created_at=recent))
        for _ in range(2):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note="b old", entry_ids=[e_b.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=ancient))
        # e_c: 3 recent journals
        for i in range(3):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note=f"c journal {i}", entry_ids=[e_c.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=recent))
        # e_d: 5 recent journals (but lifecycle=archived → ineligible)
        for i in range(5):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note=f"d journal {i}", entry_ids=[e_d.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=recent))
        # e_e: 3 recent journals (but soft-deleted → ineligible)
        for i in range(3):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note=f"e journal {i}", entry_ids=[e_e.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=recent))
        # e_f: 3 recent journals → eligible, but LLM will return same extra
        for i in range(3):
            s.add(Journal(id=new_id(), conversation_id=conv.id,
                          note=f"f journal {i}", entry_ids=[e_f.id],
                          tags=[], source_kind="reflect_turn",
                          created_at=recent))

        await s.commit()
        return {
            "e_a": e_a.id, "e_b": e_b.id, "e_c": e_c.id,
            "e_d": e_d.id, "e_e": e_e.id, "e_f": e_f.id,
        }


async def main():
    await _create_schema()
    seeded = await _seed()
    factory = get_session_factory()

    plan = {
        seeded["e_a"]: "A integrated insight from journals — Raft consensus revisited 4 times.",
        seeded["e_c"]: "C integrated insight — discussed across 3 conversations.",
        seeded["e_f"]: "F unchanged extra",  # same as current → noop
    }
    fake = _make_fake(plan)
    _install_fake(fake)

    from marginalia.tasks.handlers.refresh_entry_extra import (
        handle_refresh_entry_extra,
    )

    # ---- 1. first run ----------------------------------------------------
    await handle_refresh_entry_extra({})

    async with factory() as s:
        e_a = await s.get(FileEntry, seeded["e_a"])
        e_b = await s.get(FileEntry, seeded["e_b"])
        e_c = await s.get(FileEntry, seeded["e_c"])
        e_d = await s.get(FileEntry, seeded["e_d"])
        e_e = await s.get(FileEntry, seeded["e_e"])
        e_f = await s.get(FileEntry, seeded["e_f"])

        # 1.a Eligible+changed → updated
        assert "integrated insight" in (e_a.extra or "")
        print(f"[1] e_a updated: {e_a.extra[:60]}...")

        # 1.b e_b ineligible (only 1 recent journal) → unchanged
        assert e_b.extra == "B original extra"
        print("[2] e_b unchanged (insufficient recent journals)")

        # 1.c e_c manual_active eligible → updated
        assert "integrated insight" in (e_c.extra or "")
        print(f"[3] e_c (manual_active) updated: {e_c.extra[:60]}...")

        # 1.d e_d archived → unchanged
        assert e_d.extra == "D original extra"
        print("[4] e_d archived → unchanged")

        # 1.e e_e soft-deleted → unchanged
        assert e_e.extra == "E original extra"
        print("[5] e_e soft-deleted → unchanged")

        # 1.f e_f returned same → no UPDATE
        assert e_f.extra == "F unchanged extra"
        print("[6] e_f LLM returned identical extra → noop")

        # 2. audit + task_outcomes
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events "
            "WHERE kind = 'entry_extra_refreshed' GROUP BY kind"
        ))).all()
        # 2 actual writes (e_a, e_c)
        assert kinds == [("entry_extra_refreshed", 2)], f"audit: {kinds}"
        print(f"[7] audit entry_extra_refreshed: {kinds[0][1]}")

        outcomes = (await s.execute(text(
            "SELECT object_kind, outcome, COUNT(*) FROM task_outcomes "
            "WHERE task_kind='refresh_entry_extra' "
            "GROUP BY object_kind, outcome"
        ))).all()
        breakdown = {(ok, o): c for ok, o, c in outcomes}
        print(f"[8] outcomes breakdown: {breakdown}")
        assert breakdown.get(("file_entry", "applied")) == 2
        assert breakdown.get(("file_entry", "noop")) == 1   # e_f
        assert breakdown.get(("global", "applied")) == 1

    # ---- 3. dry_run -----------------------------------------------------
    # Force a fresh extra to be eligible again — set e_a back to old text
    async with factory() as s:
        await s.execute(
            text("UPDATE file_entries SET extra='A original extra' WHERE id=:i"),
            {"i": seeded["e_a"]},
        )
        await s.commit()

    CALL_LOG.clear()
    await handle_refresh_entry_extra({"dry_run": True})

    async with factory() as s:
        e_a = await s.get(FileEntry, seeded["e_a"])
        # dry_run → not actually written
        assert e_a.extra == "A original extra"
        print("[9] dry_run: e_a NOT updated (correct)")

        # but a task_outcomes row exists with reason=dry_run
        rows = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='refresh_entry_extra' "
            "AND object_id=:eid "
            "ORDER BY completed_at DESC LIMIT 1"
        ), {"eid": seeded["e_a"]})).first()
        d = rows[0]
        if isinstance(d, str):
            d = json.loads(d)
        print(f"[9] dry_run outcome detail: {d}")
        assert d.get("reason") == "dry_run"
        assert d.get("would_write") is True

    print("\nALL REFRESH_ENTRY_EXTRA E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
