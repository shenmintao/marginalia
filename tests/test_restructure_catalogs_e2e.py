"""End-to-end restructure_catalogs sanity check.

Run:
    .venv/Scripts/python tests/test_restructure_catalogs_e2e.py

Verifies a mixed batch of operations:
  Initial tree:
      Research [root]
        ├── LLM
        ├── Old           ← target for soft_delete + merge_into LLM
        └── Misc          ← will be renamed to "General"
      (orphan)
        └── Tools         ← will be moved under Research

  Entries:
      e1, e2 → catalog Research/LLM
      e3, e4 → catalog Research/Old
      e5     → catalog (orphan)/Tools
      e6     → catalog Research/Misc

  Operations the LLM proposes:
    1. rename(catalog=Misc, new_name="General")
    2. move(catalog=Tools, new_parent=Research)
    3. create(temp_id="tmp_AI", name="AI", parent_id=Research)
    4. soft_delete(catalog=Old, merge_into=LLM)
    5. move_entries(entry_ids=[e1, e2], target=tmp_AI)
    6. update_extra(catalog=Research, extra="Top-level research category")
    7. (rejected) move(catalog=Research, new_parent=Tools) — would cycle
    8. (rejected) create(temp_id="tmp_AI", name="dup") — temp_id reused

After:
  - Misc renamed to "General"
  - Tools.parent_id = Research.id
  - new "AI" catalog exists under Research, has e1+e2 in it
  - Old soft-deleted (deleted_at set); its entries moved to LLM.id
  - Research.extra updated
  - the cycle move + dup-temp_id ops recorded as 'rejected' in task_outcomes
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_restructure_e2e_data"
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
from marginalia.db.models import Base, Catalog, File, FileEntry, Folder
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ---- fake LLM --------------------------------------------------------------

CALLS: list[ChatRequest] = []


def _make_fake(operations: list[dict]):
    class _Fake:
        profile_name = "ingest"
        model = "fake-model"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            CALLS.append(request)
            payload = {"operations": operations}
            return ChatResponse(
                text=json.dumps(payload),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=900, output_tokens=200, cache_read_tokens=600),
                parsed_json=payload,
            )
    return _Fake()


def _install(client) -> None:
    import marginalia.tasks.handlers.restructure_catalogs as mod
    mod.get_chat_client = lambda profile="ingest": client  # type: ignore[assignment]


# ---- seed ------------------------------------------------------------------

async def _seed():
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder)

        # one shared file
        f = File(id=new_id(), storage_key="00/aa/x",
                 sha256="z" * 64, size_bytes=10,
                 mime_type="text/plain", original_ext=".txt", kind="text",
                 summary="x", description={"sections": []}, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f)
        await s.flush()

        # catalogs
        research = Catalog(id=new_id(), parent_id=None, name="Research",
                           summary=None, description=None, extra=None, tags=None,
                           created_at=now, updated_at=now)
        s.add(research)
        await s.flush()
        llm_cat = Catalog(id=new_id(), parent_id=research.id, name="LLM",
                          summary=None, description=None, extra=None, tags=None,
                          created_at=now, updated_at=now)
        old_cat = Catalog(id=new_id(), parent_id=research.id, name="Old",
                          summary=None, description=None, extra=None, tags=None,
                          created_at=now, updated_at=now)
        misc = Catalog(id=new_id(), parent_id=research.id, name="Misc",
                       summary=None, description=None, extra=None, tags=None,
                       created_at=now, updated_at=now)
        tools = Catalog(id=new_id(), parent_id=None, name="Tools",
                        summary=None, description=None, extra=None, tags=None,
                        created_at=now, updated_at=now)
        s.add_all([llm_cat, old_cat, misc, tools])
        await s.flush()

        def _mk_entry(name, catalog_id):
            e = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                          display_name=name, lifecycle="active",
                          catalog_id=catalog_id, extra=None,
                          created_at=now, updated_at=now)
            s.add(e)
            return e

        e1 = _mk_entry("a.txt", llm_cat.id)
        e2 = _mk_entry("b.txt", llm_cat.id)
        e3 = _mk_entry("c.txt", old_cat.id)
        e4 = _mk_entry("d.txt", old_cat.id)
        e5 = _mk_entry("e.txt", tools.id)
        e6 = _mk_entry("f.txt", misc.id)

        await s.commit()
        return {
            "research": research.id, "llm": llm_cat.id, "old": old_cat.id,
            "misc": misc.id, "tools": tools.id,
            "e1": e1.id, "e2": e2.id, "e3": e3.id, "e4": e4.id,
            "e5": e5.id, "e6": e6.id,
        }


async def main():
    await _create_schema()
    seeded = await _seed()

    operations = [
        {"op": "rename", "catalog_id": seeded["misc"], "new_name": "General"},
        {"op": "move",   "catalog_id": seeded["tools"], "new_parent_id": seeded["research"]},
        {"op": "create", "temp_id": "tmp_AI", "name": "AI", "parent_id": seeded["research"]},
        {"op": "soft_delete", "catalog_id": seeded["old"], "merge_into": seeded["llm"]},
        {"op": "move_entries", "entry_ids": [seeded["e1"], seeded["e2"]],
         "target_catalog_id": "tmp_AI"},
        {"op": "update_extra", "catalog_id": seeded["research"],
         "extra": "Top-level research category"},
        # rejected: would create cycle (Research -> Tools -> Research after move-2)
        {"op": "move",   "catalog_id": seeded["research"], "new_parent_id": seeded["tools"]},
        # rejected: temp_id reused
        {"op": "create", "temp_id": "tmp_AI", "name": "dup", "parent_id": seeded["research"]},
    ]

    fake = _make_fake(operations)
    _install(fake)

    from marginalia.tasks.handlers.restructure_catalogs import handle_restructure_catalogs
    await handle_restructure_catalogs({})

    factory = get_session_factory()

    # ---- 1. rename: Misc → General -----------------------------------------
    async with factory() as s:
        misc_now = await s.get(Catalog, seeded["misc"])
        assert misc_now.name == "General", f"rename failed: {misc_now.name}"

    # ---- 2. move: Tools.parent_id = Research --------------------------------
        tools_now = await s.get(Catalog, seeded["tools"])
        assert tools_now.parent_id == seeded["research"], \
            f"move failed: {tools_now.parent_id}"

    # ---- 3. create: AI under Research, with entries ------------------------
        ai_row = (await s.execute(
            select(Catalog).where(Catalog.name == "AI", Catalog.deleted_at.is_(None))
        )).scalar_one_or_none()
        assert ai_row is not None, "create AI failed"
        assert ai_row.parent_id == seeded["research"]
        ai_id = ai_row.id

        e1_now = await s.get(FileEntry, seeded["e1"])
        e2_now = await s.get(FileEntry, seeded["e2"])
        assert e1_now.catalog_id == ai_id, f"e1 not moved: {e1_now.catalog_id}"
        assert e2_now.catalog_id == ai_id

    # ---- 4. soft_delete Old + reassign entries to LLM ----------------------
        old_now = await s.get(Catalog, seeded["old"])
        assert old_now.deleted_at is not None, "Old not soft-deleted"
        e3_now = await s.get(FileEntry, seeded["e3"])
        e4_now = await s.get(FileEntry, seeded["e4"])
        assert e3_now.catalog_id == seeded["llm"], \
            f"e3 not merged into LLM: {e3_now.catalog_id}"
        assert e4_now.catalog_id == seeded["llm"]

    # ---- 5. update_extra ---------------------------------------------------
        research_now = await s.get(Catalog, seeded["research"])
        assert research_now.extra == "Top-level research category"

    # ---- 6. cycle move rejected --------------------------------------------
        # Research.parent_id should still be NULL (cycle move was rejected)
        assert research_now.parent_id is None, \
            f"cycle move was applied! Research.parent_id={research_now.parent_id}"

    # ---- 7. audit + task_outcomes invariants -------------------------------
    async with factory() as s:
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events GROUP BY kind ORDER BY kind"
        ))).all()
        kc = {k: c for k, c in kinds}
        print("[audit] kinds:", kc)
        # catalog_updated: rename(1) + create(1) + update_extra(1) + soft_delete(1) = 4
        assert kc.get("catalog_updated") == 4, kc
        # catalog_moved: move(Tools)(1) + parent reassignments under soft_delete (Old has 0
        # children of its own — none reassigned) = 1
        assert kc.get("catalog_moved") == 1

        outs = (await s.execute(text(
            "SELECT object_kind, outcome, COUNT(*) FROM task_outcomes "
            "WHERE task_kind='restructure_catalogs' GROUP BY object_kind, outcome"
        ))).all()
        breakdown = {(ok, o): c for ok, o, c in outs}
        print("[outcomes] breakdown:", breakdown)
        # 6 successful ops on real catalogs (rename, move, create, soft_delete,
        # update_extra) — wait: move_entries also writes one catalog outcome.
        # So: rename, move, create, soft_delete, move_entries, update_extra = 6
        assert breakdown.get(("catalog", "applied")) == 6, breakdown
        # 2 rejected ops: cycle move + dup temp_id
        assert breakdown.get(("catalog_op", "rejected")) == 2, breakdown
        # 1 global summary
        assert breakdown.get(("global", "applied")) == 1, breakdown

    print("\nALL RESTRUCTURE_CATALOGS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
