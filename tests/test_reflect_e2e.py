"""End-to-end reflect_turn sanity check.

Run:
    .venv/Scripts/python tests/test_reflect_e2e.py

Verifies (post-2026-05-24 reflect_turn slim-down — see [[journal-tiers]]):
  1. Synthesize a session + a finished conversation that touched 2 entries.
  2. Stub the `reflect` LLM client to return a canned reflection containing
     ONE journal entry (the only output channel reflect_turn now produces).
  3. Run the handler. Verify writes:
     - 1 journal row with entry_ids + tags
     - task_outcomes row (task_kind='reflect_turn', object_kind='conversation')
       with detail.journal_entries == 1
     - NO entry_relations / EntryTag(source='reflect') / *_extra writes
  4. Re-run on a different conversation referencing the same pair —
     a SECOND independent journal row appears (no more pairwise increments;
     that work has moved to mine_* + vet_relations).
  5. Re-run the SAME conversation_id — idempotence kicks in (no-op).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_reflect_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm
from marginalia.db.engine import get_session_factory, get_engine
from marginalia.db.models import (
    Base, Catalog, Conversation, EntryRelation, EntryTag, FileEntry, Folder,
    Journal, Session, Tag, View,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app
from marginalia.utils.ids import new_id


# ---- fake LLM ---------------------------------------------------------------

REFLECT_CALLS: list[ChatRequest] = []


def _make_fake_reflect(entry_a: str, entry_b: str):
    payload = {
        "journal_entries": [
            {
                "question": "How do paper A and paper B compare on consensus?",
                "answer": (
                    "Both papers tackle distributed consensus but from different angles. "
                    "A focuses on leader election under crash-stop failures with a single-leader model. "
                    "B targets Byzantine acceptors and quorum intersection. "
                    "They overlap on safety arguments via majority voting; recommend reading A first as the simpler model."
                ),
                "entry_ids": [entry_a, entry_b],
                "tags": ["hint:enrich_tags"],
            }
        ],
    }

    class _FakeChatClient:
        profile_name = "reflect"
        model = "fake-reflect"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            REFLECT_CALLS.append(request)
            return ChatResponse(
                text=json.dumps(payload),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=2500, output_tokens=600, cache_read_tokens=2000),
                parsed_json=payload,
            )

    return _FakeChatClient()


def _install_fake_reflect_client(client) -> None:
    llm.reset_clients_cache()
    def _factory(profile: str = "ingest"):
        return client
    import marginalia.tasks.handlers.reflect_turn as rmod
    rmod.get_chat_client = _factory  # type: ignore[assignment]


# ---- helpers ----------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_world():
    """Seed: 1 folder, 1 catalog, 1 view, 2 entries, 1 tag (markdown/form)."""
    factory = get_session_factory()
    async with factory() as s:
        now = _now()
        folder = Folder(id=new_id(), parent_id=None, name="research",
                        created_at=now, updated_at=now)
        catalog = Catalog(id=new_id(), parent_id=None, name="Consensus",
                          summary=None, description=None, extra=None, tags=None,
                          created_at=now, updated_at=now)
        view = View(id=new_id(), name="Consensus reading list",
                    summary=None, description=None, extra=None, tags=None,
                    filter_spec={"catalog_subtree": ["root"]},
                    created_at=now, updated_at=now)
        s.add_all([folder, catalog, view])

        from marginalia.db.models import File
        f1 = File(id=new_id(), storage_key="aa/bb/k1", sha256="a"*64, size_bytes=100,
                  mime_type="text/markdown", original_ext=".md", kind="text",
                  summary="Paper A", description={"sections": []}, extra=None,
                  ingest_status="done", ingested_at=now,
                  created_at=now, updated_at=now)
        f2 = File(id=new_id(), storage_key="cc/dd/k2", sha256="b"*64, size_bytes=200,
                  mime_type="text/markdown", original_ext=".md", kind="text",
                  summary="Paper B", description={"sections": []}, extra=None,
                  ingest_status="done", ingested_at=now,
                  created_at=now, updated_at=now)
        s.add_all([f1, f2])
        await s.flush()

        e1 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f1.id,
                       display_name="paperA.md", lifecycle="active",
                       catalog_id=catalog.id, extra=None,
                       created_at=now, updated_at=now)
        e2 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f2.id,
                       display_name="paperB.md", lifecycle="active",
                       catalog_id=catalog.id, extra=None,
                       created_at=now, updated_at=now)
        s.add_all([e1, e2])

        tag_md = Tag(id=new_id(), name="markdown", facet="form",
                     alias_of=None, doc_count=5, last_used_at=now,
                     created_at=now, updated_at=now)
        s.add(tag_md)

        session_row = Session(id=new_id(), started_at=now, ended_at=_now(),
                              end_reason="normal",
                              initiating_user_message="compare paper A and B",
                              turn_count=1, total_input_tokens=0, total_output_tokens=0,
                              total_cache_read=0, total_tool_calls=2, total_llm_calls=1,
                              total_duration_ms=0)
        s.add(session_row)
        await s.flush()

        conv = Conversation(
            id=new_id(),
            session_id=session_row.id,
            turn_index=0,
            started_at=now,
            ended_at=_now(),
            user_message="Compare paper A and paper B on consensus.",
            agent_response="Paper A focuses on Raft, Paper B on Paxos; they overlap on safety.",
            tool_calls=[
                {"name": "read_entries_metadata",
                 "arguments": {"entry_ids": [e1.id, e2.id]},
                 "result": {"entries": [{"id": e1.id}, {"id": e2.id}]}},
                {"name": "read_file_section",
                 "arguments": {"entry_id": e1.id, "section_id": "s1"},
                 "result": {"text": "..."}},
            ],
            llm_calls=[{"model": "claude-opus-4-7", "input_tokens": 5000, "output_tokens": 500}],
            total_input_tokens=5000, total_output_tokens=500,
            total_tool_calls=2, total_llm_calls=1,
            total_duration_ms=0,
        )
        s.add(conv)
        await s.commit()

        return {
            "entry_a": e1.id, "entry_b": e2.id,
            "catalog_id": catalog.id, "view_id": view.id,
            "session_id": session_row.id,
            "conversation_id": conv.id,
            "preexisting_tag_md_id": tag_md.id,
        }


# ---- main -------------------------------------------------------------------

async def main():
    await _create_schema()
    seeded = await _seed_world()

    fake = _make_fake_reflect(entry_a=seeded["entry_a"], entry_b=seeded["entry_b"])
    _install_fake_reflect_client(fake)

    from marginalia.tasks.handlers.reflect_turn import handle_reflect_turn

    factory = get_session_factory()

    # --- pass 1: produce the journal write ----------------------------------
    await handle_reflect_turn({"conversation_id": seeded["conversation_id"]})
    assert len(REFLECT_CALLS) == 1, f"expected 1 reflect call, got {len(REFLECT_CALLS)}"

    async with factory() as s:
        # journal: exactly one row
        journals = (await s.execute(select(Journal).where(
            Journal.conversation_id == seeded["conversation_id"]))).scalars().all()
        assert len(journals) == 1
        j = journals[0]
        assert j.source_kind == "reflect_turn"
        assert seeded["entry_a"] in j.entry_ids and seeded["entry_b"] in j.entry_ids
        assert "hint:enrich_tags" in j.tags

        # entry_relations: NONE — reflect_turn no longer writes them
        rels = (await s.execute(select(EntryRelation))).scalars().all()
        assert len(rels) == 0, f"reflect_turn should not write entry_relations; found {len(rels)}"

        # entry_tags(source='reflect'): NONE — reflect_turn no longer writes them
        ets = (await s.execute(
            select(EntryTag).where(EntryTag.source == "reflect")
        )).scalars().all()
        assert len(ets) == 0, f"reflect_turn should not write entry_tags; found {len(ets)}"

        # file_entry / catalog / view extras: untouched
        e_a = await s.get(FileEntry, seeded["entry_a"])
        assert e_a.extra is None, f"entry.extra should remain None; got {e_a.extra!r}"
        cat = await s.get(Catalog, seeded["catalog_id"])
        assert cat.extra is None, f"catalog.extra should remain None; got {cat.extra!r}"
        view = await s.get(View, seeded["view_id"])
        assert view.extra is None, f"view.extra should remain None; got {view.extra!r}"

        # files.* must be untouched (write-once)
        from marginalia.db.models import File
        all_files = (await s.execute(select(File))).scalars().all()
        for f in all_files:
            assert f.summary in ("Paper A", "Paper B"), f"file.summary mutated: {f.summary!r}"

        rt_done = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='reflect_turn' AND object_id=:c"
        ), {"c": seeded["conversation_id"]})).scalars().all()
        assert len(rt_done) == 1
        detail = json.loads(rt_done[0]) if isinstance(rt_done[0], str) else rt_done[0]
        assert detail.get("journal_entries") == 1
        print("[pass 1] reflect_turn task_outcomes detail:", detail)

    # --- pass 2: same conversation_id → idempotence kicks in ----------------
    await handle_reflect_turn({"conversation_id": seeded["conversation_id"]})
    assert len(REFLECT_CALLS) == 1, "reflect was called twice on idempotent re-run"
    async with factory() as s:
        journals = (await s.execute(select(Journal).where(
            Journal.conversation_id == seeded["conversation_id"]))).scalars().all()
        assert len(journals) == 1, "journal duplicated on idempotent re-run"

    # --- pass 3: NEW conversation re-touching same pair → independent row --
    async with factory() as s:
        now = _now()
        conv2 = Conversation(
            id=new_id(),
            session_id=seeded["session_id"],
            turn_index=1,
            started_at=now,
            ended_at=_now(),
            user_message="Look again at A vs B.",
            agent_response="Same conclusion.",
            tool_calls=[{"name": "read_entries_metadata",
                         "arguments": {"entry_ids": [seeded["entry_a"], seeded["entry_b"]]},
                         "result": {}}],
            llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=1, total_llm_calls=0,
            total_duration_ms=0,
        )
        s.add(conv2)
        await s.commit()
        conv2_id = conv2.id

    await handle_reflect_turn({"conversation_id": conv2_id})
    assert len(REFLECT_CALLS) == 2

    async with factory() as s:
        # second journal row exists; INDEPENDENT (not a merge into the first)
        journals_all = (await s.execute(select(Journal))).scalars().all()
        assert len(journals_all) == 2
        assert {j.conversation_id for j in journals_all} == {
            seeded["conversation_id"], conv2_id,
        }
        # entry_relations still empty — pairwise observation_count is now
        # exclusively the territory of mine_* + vet_relations.
        rels = (await s.execute(select(EntryRelation))).scalars().all()
        assert len(rels) == 0

    print("\nALL REFLECT E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
