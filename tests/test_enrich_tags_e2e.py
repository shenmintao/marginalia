"""End-to-end enrich_tags sanity check.

Run:
    .venv/Scripts/python tests/test_enrich_tags_e2e.py

Verifies:
  1. Eligibility filtering:
     - 4 entries with lifecycle ∈ ('active', 'manual_active') AND no recent
       task_outcomes(task_kind='enrich_tags', object_id=entry_id) AND parent
       file ingest_status='done' → all eligible
     - 1 'manual_archived' entry → excluded by lifecycle
     - 1 'active' entry with a recent task_outcomes row → excluded
     - 1 'active' entry whose file is still ingest_status='processing' →
       excluded
  2. Strict-vocabulary enforcement:
     - LLM returns valid ids + 1 BOGUS id "totally-fake-id" → backend
       silently drops the bogus id; recorded in the per-entry task_outcomes
       detail under tag_ids_proposed_but_dropped
     - LLM returns the SAME valid id twice in one call → backend dedups
       within the call (no PK collision)
     - LLM returns an id the entry already has via 'ingest' → recorded
       under tag_ids_already_present, not duplicated
  3. INSERTed rows have source='enrich_tags' and the (entry, tag) PK is
     respected.
  4. task_outcomes: one per processed entry (object_kind='file_entry') +
     one summary (object_kind='global').
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_enrich_tags_e2e_data"
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
    Base, EntryTag, File, FileEntry, Folder, Tag, TaskOutcome,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.utils.ids import new_id

CALL_LOG: list[ChatRequest] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_fake_llm(plan: dict[str, list[str]]):
    """plan: entry_id → list of tag_ids to suggest (may include bogus)."""
    class _Fake:
        profile_name = "ingest"
        model = "fake-model"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            CALL_LOG.append(request)
            user_text = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
            ctx_start = user_text.index("<context>") + len("<context>")
            ctx_end = user_text.index("</context>")
            ctx_blob = user_text[ctx_start:ctx_end].strip()
            ctx = json.loads(ctx_blob)
            assignments = []
            for e in ctx["entries"]:
                eid = e["entry_id"]
                if eid in plan:
                    assignments.append({"entry_id": eid, "tag_ids": plan[eid]})
                else:
                    assignments.append({"entry_id": eid, "tag_ids": []})
            payload = {"assignments": assignments}
            return ChatResponse(
                text=json.dumps(payload),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=600, output_tokens=120, cache_read_tokens=400),
                parsed_json=payload,
            )

    return _Fake()


def _install(client) -> None:
    import marginalia.tasks.handlers.enrich_tags as mod
    mod.get_chat_client = lambda profile="ingest": client  # type: ignore[assignment]


# ---- seed -------------------------------------------------------------------

async def _seed():
    """7 entries across the various eligibility states."""
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder)

        # one ingested file
        f_done = File(id=new_id(), storage_key="00/aa/done",
                      sha256="d"*64, size_bytes=10,
                      mime_type="text/plain", original_ext=".txt", kind="text",
                      summary="A note about consensus algorithms.",
                      description={"sections": [
                          {"title": "Raft", "key_terms": ["leader", "log replication"]},
                          {"title": "Paxos", "key_terms": ["acceptor", "proposer"]},
                      ]},
                      extra=None,
                      ingest_status="done", ingested_at=now,
                      created_at=now, updated_at=now)
        # one not-yet-ingested file
        f_pending = File(id=new_id(), storage_key="00/aa/pending",
                         sha256="p"*64, size_bytes=10,
                         mime_type="text/plain", original_ext=".txt", kind=None,
                         summary=None, description=None, extra=None,
                         ingest_status="processing", ingested_at=None,
                         created_at=now, updated_at=now)
        s.add_all([f_done, f_pending])
        await s.flush()

        # canonical vocabulary
        tags = {}
        for name, facet in [
            ("consensus", "topic"),
            ("raft", "topic"),
            ("paxos", "topic"),
            ("markdown", "form"),
            ("english", "language"),
        ]:
            t = Tag(id=new_id(), name=name, facet=facet, alias_of=None,
                    doc_count=10, last_used_at=now,
                    created_at=now, updated_at=now)
            s.add(t)
            tags[name] = t
        # an alias tag — must NOT be offered to the LLM
        alias = Tag(id=new_id(), name="md", facet="form", alias_of=tags["markdown"].id,
                    doc_count=0, last_used_at=now,
                    created_at=now, updated_at=now)
        s.add(alias)
        await s.flush()

        # entries
        def _mk(name, lifecycle, file_id):
            e = FileEntry(id=new_id(), folder_id=folder.id, file_id=file_id,
                          display_name=name, lifecycle=lifecycle,
                          created_at=now, updated_at=now)
            s.add(e)
            return e

        e_active1 = _mk("active1.md", "active", f_done.id)
        e_active2 = _mk("active2.md", "active", f_done.id)
        e_active3 = _mk("active3.md", "active", f_done.id)
        e_manual_active = _mk("manual.md", "manual_active", f_done.id)
        e_archived = _mk("old.md", "manual_archived", f_done.id)
        e_recent_enrich = _mk("recent.md", "active", f_done.id)
        e_pending = _mk("pending.md", "active", f_pending.id)
        await s.flush()

        # entries already have one tag each (existing 'consensus' or 'markdown')
        s.add_all([
            EntryTag(entry_id=e_active1.id, tag_id=tags["consensus"].id,
                     source="ingest", created_at=now),
            EntryTag(entry_id=e_active2.id, tag_id=tags["markdown"].id,
                     source="ingest", created_at=now),
        ])

        # recent task_outcomes row → excludes e_recent_enrich
        s.add(TaskOutcome(
            id=new_id(),
            task_kind="enrich_tags",
            object_kind="file_entry",
            object_id=e_recent_enrich.id,
            outcome="applied",
            detail={"tag_ids_added": []},
            completed_at=_now() - timedelta(days=2),
        ))

        await s.commit()
        return {
            "entries": {
                "active1": e_active1.id,
                "active2": e_active2.id,
                "active3": e_active3.id,
                "manual_active": e_manual_active.id,
                "archived": e_archived.id,
                "recent_enrich": e_recent_enrich.id,
                "pending": e_pending.id,
            },
            "tags": {n: t.id for n, t in tags.items()},
            "alias_md_id": alias.id,
        }


# ---- main -------------------------------------------------------------------

async def main():
    await _create_schema()
    seeded = await _seed()
    entries = seeded["entries"]
    tags = seeded["tags"]

    # Plan: each eligible entry gets some real picks + 1 bogus + maybe an
    # already-present tag and a duplicate tag in same call. Ineligible
    # entries should NEVER appear in calls.
    plan = {
        entries["active1"]: [tags["raft"], tags["paxos"], "totally-fake-id"],
        entries["active2"]: [tags["consensus"], tags["english"], tags["markdown"]],
        # active3 has 'consensus' nowhere yet; pick it twice in same call to
        # exercise in-call dedup
        entries["active3"]: [tags["raft"], tags["english"], tags["english"]],
        entries["manual_active"]: [tags["markdown"]],
    }
    fake = _make_fake_llm(plan)
    _install(fake)

    from marginalia.tasks.handlers.enrich_tags import handle_enrich_tags
    await handle_enrich_tags({})

    factory = get_session_factory()
    async with factory() as s:
        # 1. only the eligible 4 should have been processed (one task_outcomes
        #    row per processed entry, plus one summary row)
        per_entry_outcomes = (await s.execute(text(
            "SELECT object_id FROM task_outcomes "
            "WHERE task_kind='enrich_tags' AND object_kind='file_entry'"
        ))).scalars().all()
        seen_entry_ids = set(per_entry_outcomes)
        # The seeded "recent enrich" outcome is also in this set; subtract it
        # so we only assert about THIS run.
        seen_entry_ids.discard(entries["recent_enrich"])
        print("[1] enriched entry ids:", sorted(seen_entry_ids))
        expected_eligible = {entries["active1"], entries["active2"],
                             entries["active3"], entries["manual_active"]}
        assert seen_entry_ids == expected_eligible, (
            f"unexpected eligible set; missing: {expected_eligible - seen_entry_ids} "
            f"extra: {seen_entry_ids - expected_eligible}")
        # ineligible never appears
        for k in ("archived", "pending"):
            assert entries[k] not in seen_entry_ids, f"{k} should have been skipped"

        # 2. vocabulary feed: alias 'md' must NOT be offered. Inspect call log.
        assert len(CALL_LOG) >= 1
        call_text = CALL_LOG[0].messages[0].content[0].text  # type: ignore[index, attr-defined]
        assert seeded["alias_md_id"] not in call_text, "alias tag id leaked to LLM"

        # 3. entry_tags after run
        et = (await s.execute(
            select(EntryTag.entry_id, EntryTag.tag_id, EntryTag.source)
        )).all()
        et_map: dict[str, list[tuple[str, str]]] = {}
        for eid, tid, src in et:
            et_map.setdefault(eid, []).append((tid, src))
        print("[2] entry_tags by entry:")
        for eid, lst in et_map.items():
            short = next(k for k, v in entries.items() if v == eid)
            print(f"    {short}: {lst}")

        # active1: had 'consensus' (ingest). Adds: raft, paxos. Bogus dropped.
        et_active1 = {(tid, src) for tid, src in et_map[entries["active1"]]}
        assert (tags["consensus"], "ingest") in et_active1
        assert (tags["raft"], "enrich_tags") in et_active1
        assert (tags["paxos"], "enrich_tags") in et_active1
        assert "totally-fake-id" not in {tid for tid, _ in et_active1}
        assert len(et_active1) == 3

        # active2: had 'markdown' (ingest). Picks: consensus, english, markdown.
        # markdown is already present → skipped silently. Result: + consensus, english.
        et_active2 = {(tid, src) for tid, src in et_map[entries["active2"]]}
        assert (tags["markdown"], "ingest") in et_active2
        assert (tags["consensus"], "enrich_tags") in et_active2
        assert (tags["english"], "enrich_tags") in et_active2
        assert len(et_active2) == 3, f"active2 entry_tags count wrong: {et_active2}"

        # active3: had no tags. Adds raft + english (dup english collapsed).
        et_active3 = {(tid, src) for tid, src in et_map[entries["active3"]]}
        assert (tags["raft"], "enrich_tags") in et_active3
        assert (tags["english"], "enrich_tags") in et_active3
        assert len(et_active3) == 2

        # manual_active: had no tags. Adds markdown.
        et_manual = {(tid, src) for tid, src in et_map.get(entries["manual_active"], [])}
        assert (tags["markdown"], "enrich_tags") in et_manual
        assert len(et_manual) == 1

        # excluded entries have no entry_tags
        for k in ("archived", "recent_enrich", "pending"):
            assert entries[k] not in et_map, f"{k} got entry_tags"

        # 4. summary task_outcomes
        summary = (await s.execute(text(
            "SELECT detail FROM task_outcomes "
            "WHERE task_kind='enrich_tags' AND object_kind='global'"
        ))).all()
        print("[3] enrich_tags summary outcomes:", summary)
        assert len(summary) == 1
        payload = summary[0][0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        assert payload["candidates"] == 4
        assert payload["entries_enriched"] == 4
        # active1: +2 (raft, paxos), active2: +2 (consensus, english),
        # active3: +2 (raft, english), manual_active: +1 (markdown). Total: 7
        assert payload["tags_added"] == 7

        # 5. per-entry task_outcomes record dropped + already-present ids
        outcome_rows = (await s.execute(text(
            "SELECT object_id, detail FROM task_outcomes "
            "WHERE task_kind='enrich_tags' AND object_kind='file_entry' "
            "AND completed_at >= :c"
        ), {"c": _now() - timedelta(minutes=5)})).all()

        def _detail_of(eid):
            for oid, det in outcome_rows:
                if oid == eid:
                    return det if isinstance(det, dict) else json.loads(det)
            return None

        eactive1_detail = _detail_of(entries["active1"])
        assert eactive1_detail is not None
        assert "totally-fake-id" in eactive1_detail["tag_ids_proposed_but_dropped"]

        # active2 was given 'markdown' but already had it from ingest →
        # tag_ids_already_present should record it
        eactive2_detail = _detail_of(entries["active2"])
        assert eactive2_detail is not None
        assert tags["markdown"] in eactive2_detail["tag_ids_already_present"], \
            f"markdown should be in already_present, got {eactive2_detail}"

        # active3: english appeared twice in the LLM call → only one row
        n_english_active3 = sum(
            1 for tid, _ in et_map[entries["active3"]] if tid == tags["english"]
        )
        assert n_english_active3 == 1, f"english duplicated for active3: {n_english_active3}"

    print("\nALL ENRICH_TAGS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
