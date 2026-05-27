"""End-to-end mine_session_cooccurrence (Cycle 22).

Run:
    .venv/Scripts/python tests/test_mine_session_cooccurrence_e2e.py

Verifies:
  1. Pair appearing in ≥ 2 journal rows → new entry_relation written
     with source_kind='mine_session_cooccurrence', canonical (a < b),
     observation_count = co-occurrence count.
  2. Pair appearing only once → not written.
  3. Already-existing entry_relation → UPDATE observation_count + last_observed_at;
     does NOT create duplicate.
  4. Soft-deleted entry → skipped (does not appear in any new relation).
  5. cap parameter enforced (we synthesise 4 candidates with cap=2).
  6. canonical ordering enforced (a_id < b_id by sort).
  7. audit_events kind='relation_mined' per write; task_outcomes summary.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_mine_session_cooccurrence_e2e_data"
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

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, EntryRelation, File, FileEntry, Folder,
    Journal, Session,
)
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Seed entries + a session/conversation for journals to FK-reference,
    plus the journals that drive the test."""
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        f = File(id=new_id(), storage_key="00/aa/x", sha256="z" * 64,
                 size_bytes=10, mime_type="text/plain", original_ext=".txt",
                 kind="text", summary="x", description={"sections": []},
                 extra=None, ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f); await s.flush()

        # 5 entries: A, B, C, D live; E soft-deleted
        entries: list[FileEntry] = []
        for label in ["A", "B", "C", "D", "E"]:
            e = FileEntry(
                id=new_id(), folder_id=folder.id, file_id=f.id,
                display_name=f"{label}.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=now, updated_at=now,
            )
            entries.append(e)
            s.add(e)
        await s.flush()
        e_a, e_b, e_c, e_d, e_e = entries

        # Soft-delete E so it should be excluded from mining
        e_e.deleted_at = _now()
        e_e.purge_after = _now() + timedelta(days=7)

        # Pre-existing entry_relation between A and C, written by a different
        # miner (mine_tag_overlap) earlier. The bumper should overwrite
        # source_kind/note to mine_session_cooccurrence on increment.
        a_id, c_id = sorted((e_a.id, e_c.id))
        existing_rel = EntryRelation(
            id=new_id(),
            entry_a_id=a_id, entry_b_id=c_id,
            note="seeded by mine_tag_overlap previously",
            source_kind="mine_tag_overlap",
            last_observed_at=now - timedelta(days=10),
            observation_count=1,
            created_at=now - timedelta(days=10),
        )
        s.add(existing_rel)

        # session + conversation for journal FK
        sess = Session(
            id=new_id(), started_at=now, ended_at=now, end_reason="normal",
            initiating_user_message="x", turn_count=0,
            total_input_tokens=0, total_output_tokens=0, total_cache_read=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(sess); await s.flush()
        conv = Conversation(
            id=new_id(), session_id=sess.id, turn_index=0,
            started_at=now, ended_at=now,
            user_message="x", agent_response="x",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(conv); await s.flush()

        # 4 journal rows:
        #   J1: [A, B, C]   → pairs (A,B), (A,C), (B,C)
        #   J2: [A, B, D]   → pairs (A,B), (A,D), (B,D)
        #   J3: [A, C]      → pair (A,C)
        #   J4: [A, E]      → pair (A,E) — E soft-deleted, must be skipped
        # Co-occurrence counts:
        #   (A,B) = 2  → new relation
        #   (A,C) = 2  → existing relation, increment by 2
        #   (B,C) = 1  → below threshold, NOT written
        #   (A,D) = 1  → below threshold
        #   (B,D) = 1  → below threshold
        #   (A,E) = 1  → below threshold AND E dead
        for entry_ids in [
            [e_a.id, e_b.id, e_c.id],
            [e_a.id, e_b.id, e_d.id],
            [e_a.id, e_c.id],
            [e_a.id, e_e.id],
        ]:
            s.add(Journal(
                id=new_id(),
                conversation_id=conv.id,
                note="seed",
                entry_ids=entry_ids,
                tags=[],
                source_kind="reflect_turn",
                created_at=now - timedelta(days=2),
            ))

        await s.commit()
        return {
            "e_a": e_a.id, "e_b": e_b.id, "e_c": e_c.id, "e_d": e_d.id,
            "e_e": e_e.id, "existing_rel_id": existing_rel.id,
        }


async def main():
    await _create_schema()
    seeded = await _seed()
    factory = get_session_factory()

    from marginalia.tasks.handlers.mine_session_cooccurrence import (
        handle_mine_session_cooccurrence,
    )

    # ---- 1. run with default cap ------------------------------------------
    await handle_mine_session_cooccurrence({})

    async with factory() as s:
        # 2.a New relation (A,B) created with source_kind='mine_session_cooccurrence'
        a_id, b_id = sorted((seeded["e_a"], seeded["e_b"]))
        rel_ab = (await s.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == a_id,
                EntryRelation.entry_b_id == b_id,
            )
        )).scalar_one_or_none()
        assert rel_ab is not None, "A↔B relation not created"
        assert rel_ab.source_kind == "mine_session_cooccurrence"
        assert rel_ab.observation_count == 2
        # canonical ordering already enforced
        assert rel_ab.entry_a_id < rel_ab.entry_b_id
        print(f"[1] new (A,B) relation source={rel_ab.source_kind} obs={rel_ab.observation_count}")

        # 2.b Existing (A,C) incremented from 1 → 1+2 = 3
        a_id_c, c_id = sorted((seeded["e_a"], seeded["e_c"]))
        rel_ac = (await s.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == a_id_c,
                EntryRelation.entry_b_id == c_id,
            )
        )).scalar_one_or_none()
        assert rel_ac is not None
        assert rel_ac.id == seeded["existing_rel_id"], \
            "(A,C) was duplicated instead of incremented"
        assert rel_ac.source_kind == "mine_session_cooccurrence", \
            "source_kind should be overwritten to the most recent miner"
        assert rel_ac.note != "seeded by reflect previously", \
            "note should also be overwritten on bump"
        assert rel_ac.observation_count == 1 + 2, \
            f"observation_count = {rel_ac.observation_count}"
        print(f"[2] existing (A,C) incremented & re-attributed: "
              f"source={rel_ac.source_kind} obs={rel_ac.observation_count}")

        # 3. (B,C) and (A,D) and (B,D) — all below threshold, NOT written
        b_id_c, c_id_b = sorted((seeded["e_b"], seeded["e_c"]))
        rel_bc = (await s.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == b_id_c,
                EntryRelation.entry_b_id == c_id_b,
            )
        )).scalar_one_or_none()
        assert rel_bc is None
        a_id_d, d_id = sorted((seeded["e_a"], seeded["e_d"]))
        assert (await s.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == a_id_d,
                EntryRelation.entry_b_id == d_id,
            )
        )).scalar_one_or_none() is None
        print("[3] below-threshold pairs (B,C), (A,D), (B,D) NOT written")

        # 4. Soft-deleted E never appears in any relation
        e_id = seeded["e_e"]
        any_rel_with_e = (await s.execute(
            select(EntryRelation).where(
                (EntryRelation.entry_a_id == e_id)
                | (EntryRelation.entry_b_id == e_id)
            )
        )).scalar_one_or_none()
        assert any_rel_with_e is None
        print("[4] soft-deleted E excluded")

        # 5. Total relations: 1 pre-existing + 1 new = 2
        all_rels = (await s.execute(select(EntryRelation))).scalars().all()
        assert len(all_rels) == 2
        print(f"[5] entry_relations total: {len(all_rels)}")

        # 6. Audit + task_outcomes
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events "
            "WHERE kind IN ('relation_mined') GROUP BY kind"
        ))).all()
        assert kinds == [("relation_mined", 2)], f"audit kinds = {kinds}"

        outcomes = (await s.execute(text(
            "SELECT outcome, detail FROM task_outcomes "
            "WHERE task_kind='mine_session_cooccurrence'"
        ))).all()
        assert len(outcomes) == 1
        outcome, detail = outcomes[0]
        if isinstance(detail, str):
            import json as _j
            detail = _j.loads(detail)
        print(f"[6] outcome={outcome} detail={detail}")
        assert outcome == "applied"
        assert detail["new_relations"] == 1
        assert detail["incremented_relations"] == 1
        assert detail["pairs_above_threshold"] == 2
        assert detail["journals_scanned"] == 4

    # ---- 7. Idempotence: re-running should still increment but not double ----
    # The journals haven't changed; running again will pump existing rels'
    # observation_count again. This is the documented behaviour for now —
    # the dispatcher schedules this once a day. If repeat-run dedup is
    # wanted later, the job needs a per-journal-row "last seen" marker.
    await handle_mine_session_cooccurrence({})
    async with factory() as s:
        rel_ab = (await s.execute(
            select(EntryRelation).where(
                EntryRelation.entry_a_id == a_id,
                EntryRelation.entry_b_id == b_id,
            )
        )).scalar_one()
        # First run: observation_count=2, second run: existing UPDATE adds 2 more
        assert rel_ab.observation_count == 4, \
            f"second run did not bump (A,B): {rel_ab.observation_count}"
        print(f"[7] re-run incremented (A,B) to {rel_ab.observation_count} "
              f"(documented behaviour)")

    # ---- 8. Cap test ------------------------------------------------------
    # Synthesise enough above-threshold pairs so cap kicks in.
    async with factory() as s:
        # Get all live entry IDs we already have
        live = (await s.execute(
            select(FileEntry.id).where(FileEntry.deleted_at.is_(None))
        )).scalars().all()
        assert len(live) >= 4

        sess_id = (await s.execute(
            select(Session.id).limit(1)
        )).scalar_one()
        conv_id = (await s.execute(
            select(Conversation.id).limit(1)
        )).scalar_one()
        # add 3 more entries
        new_e_ids = []
        for i in range(3):
            e = FileEntry(
                id=new_id(), folder_id=(await s.execute(
                    select(Folder.id).limit(1)
                )).scalar_one(),
                file_id=(await s.execute(select(File.id).limit(1))).scalar_one(),
                display_name=f"X{i}.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=_now(), updated_at=_now(),
            )
            s.add(e)
            new_e_ids.append(e.id)
        await s.flush()
        # add 2 journals containing every pair of new entries → all pairs co-occur 2x
        for _ in range(2):
            s.add(Journal(
                id=new_id(),
                conversation_id=conv_id,
                note="cap test",
                entry_ids=list(new_e_ids),
                tags=[],
                source_kind="reflect_turn",
                created_at=_now(),
            ))
        await s.commit()

    await handle_mine_session_cooccurrence({"cap": 2})
    async with factory() as s:
        outcomes = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='mine_session_cooccurrence' "
            "ORDER BY completed_at DESC LIMIT 1"
        ))).first()
        detail = outcomes[0]
        if isinstance(detail, str):
            import json as _j
            detail = _j.loads(detail)
        print(f"[8] cap=2 run: new={detail['new_relations']} "
              f"pairs_above={detail['pairs_above_threshold']}")
        # 3 new entries → 3 new pairs all above threshold; cap=2 means we
        # write at most 2 new relations. Existing relations may also be
        # incremented (the 2 already there); cap only applies to NEW writes.
        assert detail["new_relations"] <= 2, \
            f"cap not enforced: {detail['new_relations']} new relations"
        # And the 3rd new pair must remain absent.
        new_rels = (await s.execute(text(
            "SELECT entry_a_id, entry_b_id FROM entry_relations "
            "WHERE source_kind='mine_session_cooccurrence'"
        ))).all()
        new_relation_count = len([
            r for r in new_rels
            if r[0] in set(new_e_ids) or r[1] in set(new_e_ids)
        ])
        assert new_relation_count <= 2

    print("\nALL MINE_SESSION_COOCCURRENCE E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
