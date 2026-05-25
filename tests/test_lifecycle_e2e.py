"""End-to-end lifecycle (suggest_demotion + suggest_archival) sanity check.

Run:
    .venv/Scripts/python tests/test_lifecycle_e2e.py

Verifies:
  Demotion (active → demoted):
    - active entry, no journal mention, created 30+ days ago → DEMOTED
    - active entry, recent journal mention                  → unchanged
    - active entry, no journal but only 5 days old           → unchanged (too fresh)
    - manual_active entry that meets every criterion         → unchanged (locked)

  Archival (demoted → archived):
    - demoted entry whose updated_at is 60+ days old, no journal mention
                                                              → ARCHIVED
    - demoted entry recently demoted (updated_at fresh)       → unchanged
    - demoted entry with recent journal mention               → unchanged
    - manual_archived entry                                   → unchanged
    - active entry (regardless of age)                        → unchanged

  Audit:
    - lifecycle_changed audit row for each successful transition

  task_outcomes:
    - one row per processed entry (file_entry, applied/deferred)
    - one summary row (global, applied or noop)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_lifecycle_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select, text, update

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, File, FileEntry, Folder, Journal, Session,
)
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Seed entries spanning every state we care about."""
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder)

        # one file is enough; we vary state through file_entries
        f = File(id=new_id(), storage_key="00/aa/x",
                 sha256="z" * 64, size_bytes=10,
                 mime_type="text/plain", original_ext=".txt", kind="text",
                 summary="x", description={"sections": []}, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add(f)
        await s.flush()

        long_ago = now - timedelta(days=60)
        very_long_ago = now - timedelta(days=120)

        def _mk(name, lifecycle, created_at, updated_at):
            e = FileEntry(
                id=new_id(),
                folder_id=folder.id, file_id=f.id,
                display_name=name, lifecycle=lifecycle,
                catalog_id=None, extra=None,
                created_at=created_at, updated_at=updated_at,
            )
            s.add(e)
            return e

        # --- demotion fixtures ---
        e_demote = _mk("demote.txt", "active", long_ago, long_ago)
        e_active_used = _mk("used.txt", "active", long_ago, long_ago)
        e_too_fresh = _mk("fresh.txt", "active", now - timedelta(days=5), now - timedelta(days=5))
        e_manual_active = _mk("manual.txt", "manual_active", long_ago, long_ago)

        # --- archival fixtures ---
        e_archive = _mk("archive.txt", "demoted", very_long_ago,
                        now - timedelta(days=60))  # stably demoted 60 days
        e_recent_demoted = _mk("recent_demoted.txt", "demoted", very_long_ago,
                               now - timedelta(days=10))  # too fresh in demoted
        e_demoted_used = _mk("demoted_used.txt", "demoted", very_long_ago,
                             now - timedelta(days=60))  # stably demoted but used
        e_manual_archived = _mk("man_arch.txt", "manual_archived", very_long_ago,
                                now - timedelta(days=60))

        await s.flush()

        # --- journal: mentions e_active_used (recent) and e_demoted_used (recent)
        session_row = Session(
            id=new_id(), started_at=now, ended_at=now,
            end_reason="normal", initiating_user_message="",
            turn_count=0, total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        )
        s.add(session_row)
        await s.flush()
        conv = Conversation(
            id=new_id(), session_id=session_row.id, turn_index=0,
            started_at=now, ended_at=now,
            user_message="", agent_response="",
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(conv)
        await s.flush()

        # journal mention within 14 days → counted as "recent activity"
        s.add(Journal(
            id=new_id(),
            conversation_id=conv.id,
            note="touched used.txt and demoted_used.txt",
            entry_ids=[e_active_used.id, e_demoted_used.id],
            tags=[],
            source_kind="reflect_turn",
            created_at=now - timedelta(days=2),
        ))
        # an old journal that should NOT count
        s.add(Journal(
            id=new_id(),
            conversation_id=conv.id,
            note="ancient activity",
            entry_ids=[e_demote.id],  # this is in the past, don't save it
            tags=[],
            source_kind="reflect_turn",
            created_at=now - timedelta(days=120),
        ))

        await s.commit()
        return {
            "demote": e_demote.id,
            "active_used": e_active_used.id,
            "too_fresh": e_too_fresh.id,
            "manual_active": e_manual_active.id,
            "archive": e_archive.id,
            "recent_demoted": e_recent_demoted.id,
            "demoted_used": e_demoted_used.id,
            "manual_archived": e_manual_archived.id,
        }


async def _state(entry_id: str) -> str:
    factory = get_session_factory()
    async with factory() as s:
        return (await s.execute(
            select(FileEntry.lifecycle).where(FileEntry.id == entry_id)
        )).scalar_one()


async def main():
    await _create_schema()
    seeded = await _seed()
    factory = get_session_factory()

    from marginalia.tasks.handlers.suggest_lifecycle import (
        handle_suggest_lifecycle,
    )

    # --- 1. demote phase ---------------------------------------------------
    await handle_suggest_lifecycle({"phases": ["demote"]})

    state = {k: await _state(v) for k, v in seeded.items()}
    print("[1] state after demotion:", state)

    assert state["demote"] == "demoted", \
        f"demote should have been demoted, got {state['demote']}"
    assert state["active_used"] == "active", \
        f"active_used should be unchanged: {state['active_used']}"
    assert state["too_fresh"] == "active", \
        f"too_fresh should be unchanged: {state['too_fresh']}"
    assert state["manual_active"] == "manual_active", \
        f"manual_active must NEVER auto-change: {state['manual_active']}"
    # demoted ones not yet touched by archival
    assert state["archive"] == "demoted"
    assert state["recent_demoted"] == "demoted"
    assert state["demoted_used"] == "demoted"
    assert state["manual_archived"] == "manual_archived"

    # --- 2. archive phase --------------------------------------------------
    # The newly-demoted "demote" entry has updated_at = now, so it is too
    # fresh to be archived in the same run. We adjust its updated_at to
    # simulate "stably demoted for 60 days" so it picks up archival path
    # for fixture e_archive only — leave 'demote' alone (it's just transitioned).
    await handle_suggest_lifecycle({"phases": ["archive"]})

    state = {k: await _state(v) for k, v in seeded.items()}
    print("[2] state after archival:", state)

    assert state["archive"] == "archived", \
        f"archive should have been archived, got {state['archive']}"
    assert state["recent_demoted"] == "demoted", \
        f"recent_demoted is still too fresh: {state['recent_demoted']}"
    assert state["demoted_used"] == "demoted", \
        f"demoted_used has recent journal mention; must stay: {state['demoted_used']}"
    assert state["manual_archived"] == "manual_archived"
    # the freshly-demoted 'demote' entry was demoted just now; its updated_at
    # is too recent for archival → stays at demoted
    assert state["demote"] == "demoted"

    # --- 3. audit invariants ------------------------------------------------
    async with factory() as s:
        rows = (await s.execute(text(
            "SELECT payload FROM audit_events WHERE kind='lifecycle_changed' ORDER BY occurred_at"
        ))).scalars().all()
        print("[3] lifecycle_changed audit rows:", len(rows))
        # exactly 2: one demote, one archive
        assert len(rows) == 2, f"expected 2 lifecycle_changed audit rows, got {len(rows)}"

    # --- 4. task_outcomes ---------------------------------------------------
    async with factory() as s:
        outs = (await s.execute(text(
            "SELECT task_kind, object_kind, outcome, COUNT(*) "
            "FROM task_outcomes GROUP BY task_kind, object_kind, outcome "
            "ORDER BY task_kind, object_kind, outcome"
        ))).all()
        print("[4] task_outcomes breakdown:")
        for r in outs:
            print(f"    {r}")
        breakdown = {(tk, ok, o): c for tk, ok, o, c in outs}
        # one applied per file_entry transition, plus one global summary
        assert breakdown.get(("suggest_demotion", "file_entry", "applied")) == 1
        assert breakdown.get(("suggest_demotion", "global", "applied")) == 1
        assert breakdown.get(("suggest_archival", "file_entry", "applied")) == 1
        assert breakdown.get(("suggest_archival", "global", "applied")) == 1

    # --- 5. idempotence: re-running on the same data should be a no-op ----
    await handle_suggest_lifecycle({"phases": ["demote"]})
    await handle_suggest_lifecycle({"phases": ["archive"]})
    state = {k: await _state(v) for k, v in seeded.items()}
    # nothing flips back; manual_* still locked; protected entries still active
    assert state["manual_active"] == "manual_active"
    assert state["manual_archived"] == "manual_archived"
    assert state["active_used"] == "active"
    assert state["too_fresh"] == "active"

    async with factory() as s:
        n_global = (await s.execute(text(
            "SELECT outcome, COUNT(*) FROM task_outcomes "
            "WHERE task_kind IN ('suggest_demotion','suggest_archival') "
            "AND object_kind='global' GROUP BY outcome"
        ))).all()
        breakdown2 = {o: c for o, c in n_global}
        print("[5] global outcomes after re-run:", breakdown2)
        # 2 applied (first run) + 2 noop (second run, nothing left to do)
        assert breakdown2.get("applied") == 2
        assert breakdown2.get("noop") == 2

    print("\nALL LIFECYCLE E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
