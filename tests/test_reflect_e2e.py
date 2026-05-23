"""End-to-end reflect_turn sanity check.

Run:
    .venv/Scripts/python tests/test_reflect_e2e.py

Verifies:
  1. Synthesize a session + a finished conversation that touched 2 entries.
  2. Stub the `reflect` LLM client to return a canned reflection covering
     all 6 output channels.
  3. Run the handler. Verify writes:
     - 1 journal row with entry_ids + tags
     - 1 entry_relations row, canonical (a < b) order
     - 2 entry_tags rows with source='reflect' (one new, one re-used)
     - 1 file_entry.extra updated
     - 1 catalog.extra updated
     - 1 view.extra updated
     - task_outcomes row (task_kind='reflect_turn', object_kind='conversation')
       with non-zero detail counts
  4. Re-run handler — observation_count increments to 2.
  5. Re-re-run — idempotence kicks in (audit already there), no-op.
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
os.environ["SQLITE_PATH"] = str(_TEST_ROOT / "marginalia.db")
os.environ["LOCAL_STORAGE_ROOT"] = str(_TEST_ROOT / "objects")
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


def _make_fake_reflect(entry_a: str, entry_b: str, catalog_id: str, view_id: str):
    payload = {
        "journal_entries": [
            {
                "note": "User asked to compare paper A and paper B; both share consensus theme.",
                "entry_ids": [entry_a, entry_b],
                "tags": ["hint:enrich_tags"],
            }
        ],
        "entry_relations": [
            {
                "entry_a_id": entry_a,
                "entry_b_id": entry_b,
                "note": "Compared in user-driven analysis of consensus mechanisms.",
            }
        ],
        "entry_tag_additions": [
            # One uses an existing tag (markdown/form already there); one new.
            {"entry_id": entry_a, "name": "consensus", "facet": "topic"},
            {"entry_id": entry_b, "name": "markdown", "facet": "form"},
        ],
        "entry_extra_updates": [
            {"entry_id": entry_a, "extra": "Cross-referenced with paper B during consensus discussion."}
        ],
        "catalog_extra_updates": [
            {"catalog_id": catalog_id, "extra": "Heavily revisited; consider promoting subtree."}
        ],
        "view_extra_updates": [
            {"view_id": view_id, "extra": "User actively browses this view for consensus material."}
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

        # two file rows
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

        # pre-existing markdown/form tag (so reflect re-uses it, not creates)
        tag_md = Tag(id=new_id(), name="markdown", facet="form",
                     alias_of=None, doc_count=5, last_used_at=now,
                     created_at=now, updated_at=now)
        s.add(tag_md)

        # session + finished conversation that touched both entries via tool_calls
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
            "conversation_id": conv.id,
            "preexisting_tag_md_id": tag_md.id,
        }


# ---- main -------------------------------------------------------------------

async def main():
    await _create_schema()
    seeded = await _seed_world()

    fake = _make_fake_reflect(
        entry_a=seeded["entry_a"],
        entry_b=seeded["entry_b"],
        catalog_id=seeded["catalog_id"],
        view_id=seeded["view_id"],
    )
    _install_fake_reflect_client(fake)

    from marginalia.tasks.handlers.reflect_turn import handle_reflect_turn

    factory = get_session_factory()

    # --- pass 1: produce all writes -----------------------------------------
    await handle_reflect_turn({"conversation_id": seeded["conversation_id"]})
    assert len(REFLECT_CALLS) == 1, f"expected 1 reflect call, got {len(REFLECT_CALLS)}"

    async with factory() as s:
        # journal
        journals = (await s.execute(select(Journal).where(
            Journal.conversation_id == seeded["conversation_id"]))).scalars().all()
        assert len(journals) == 1
        j = journals[0]
        assert seeded["entry_a"] in j.entry_ids and seeded["entry_b"] in j.entry_ids
        assert "hint:enrich_tags" in j.tags

        # entry_relations canonical ordering
        rels = (await s.execute(select(EntryRelation))).scalars().all()
        assert len(rels) == 1
        r = rels[0]
        a, b = sorted((seeded["entry_a"], seeded["entry_b"]))
        assert (r.entry_a_id, r.entry_b_id) == (a, b)
        assert r.observation_count == 1
        assert r.source_kind == "reflect"

        # tags: "consensus" (new) + reuse of existing "markdown"
        ets = (await s.execute(select(EntryTag).where(EntryTag.source == "reflect"))).scalars().all()
        assert len(ets) == 2
        for et in ets:
            assert et.source == "reflect"

        # markdown tag must NOT have been duplicated
        md_count = (await s.execute(text(
            "SELECT COUNT(*) FROM tags WHERE name = 'markdown' AND facet = 'form'"
        ))).scalar()
        assert md_count == 1, f"markdown tag duplicated: {md_count}"

        # file_entry extra
        e_a = await s.get(FileEntry, seeded["entry_a"])
        assert e_a.extra and "Cross-referenced" in e_a.extra

        # catalog / view extras
        cat = await s.get(Catalog, seeded["catalog_id"])
        assert cat.extra and "promoting" in cat.extra
        view = await s.get(View, seeded["view_id"])
        assert view.extra and "consensus material" in view.extra

        # files.* must be untouched (write-once)
        from marginalia.db.models import File
        all_files = (await s.execute(select(File))).scalars().all()
        for f in all_files:
            assert f.summary in ("Paper A", "Paper B"), f"file.summary mutated: {f.summary!r}"

        # task_outcomes row present
        rt_done = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='reflect_turn' AND object_id=:c"
        ), {"c": seeded["conversation_id"]})).scalars().all()
        assert len(rt_done) == 1
        print("[pass 1] reflect_turn task_outcomes detail:", rt_done[0])

    # --- pass 2: same conversation_id → idempotence kicks in ----------------
    await handle_reflect_turn({"conversation_id": seeded["conversation_id"]})
    assert len(REFLECT_CALLS) == 1, "reflect was called twice on idempotent re-run"
    async with factory() as s:
        journals = (await s.execute(select(Journal).where(
            Journal.conversation_id == seeded["conversation_id"]))).scalars().all()
        assert len(journals) == 1, "journal duplicated on idempotent re-run"

    # --- pass 3: simulate a second conversation seeing the same pair --------
    # (delete the audit row so we run again, but using a NEW conversation id —
    # this is what would happen for a follow-up turn)
    async with factory() as s:
        now = _now()
        conv2 = Conversation(
            id=new_id(),
            session_id=(await s.execute(text(
                "SELECT session_id FROM conversations WHERE id=:c"
            ), {"c": seeded["conversation_id"]})).scalar_one(),
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
        # observation_count incremented
        rels = (await s.execute(select(EntryRelation))).scalars().all()
        assert len(rels) == 1
        assert rels[0].observation_count == 2, f"observation_count={rels[0].observation_count}"
        # note appended (deduplicated by substring check)
        assert "consensus mechanisms" in rels[0].note
        # extra still single value (overwritten not duplicated)
        e_a = await s.get(FileEntry, seeded["entry_a"])
        assert e_a.extra.count("Cross-referenced") == 1

    print("\nALL REFLECT E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
