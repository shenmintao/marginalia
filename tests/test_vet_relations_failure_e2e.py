"""End-to-end vet_relations failure paths.

Run:
    .venv/Scripts/python tests/test_vet_relations_failure_e2e.py

The happy-path test (test_vet_relations_e2e) covers verdict storage and
re-vet rules. This sibling locks down two paths that used to silently
record `outcome="applied"` even when the LLM failed:

  1. TOTAL failure: every chunk's LLM call raises. The handler must
     re-raise RuntimeError (so the task dispatcher records `last_error`
     and re-leases) AND task_outcomes gets a row with outcome="error"
     and the underlying error captured in detail.last_error.
  2. PARTIAL failure: at least one chunk succeeded so committed work is
     real and we don't raise — but task_outcomes still records
     outcome="error" with failed>0 so the user sees degraded quality.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_vet_relations_failure_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select  # noqa: E402

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import (  # noqa: E402
    Base, EntryRelation, File, FileEntry, Folder, TaskOutcome,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.tasks.handlers.vet_relations import (  # noqa: E402
    handle_vet_relations,
)
from marginalia.tasks.kinds import KIND_VET_RELATIONS  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _AlwaysFailIngest:
    """Fake LLM client whose every call raises."""
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        raise RuntimeError("simulated upstream 500")


class _PartialFailIngest:
    """Fakes one successful chunk then fails on every subsequent call.

    With BATCH_SIZE=10 and 12 candidates, the handler issues two LLM
    calls; we want the first to succeed and the second to raise.
    """
    profile_name = "ingest"
    model = "fake-ingest"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        if self.calls > 1:
            raise RuntimeError("simulated upstream 500 on chunk 2+")
        ut = request.messages[0].content[0].text  # type: ignore[attr-defined, index]
        cs = ut.index("<candidates>") + len("<candidates>")
        ce = ut.index("</candidates>")
        cands = json.loads(ut[cs:ce].strip())["candidates"]
        verdicts = [
            {
                "pair_id": c["pair_id"],
                "verdict": "yes",
                "reason": "ok",
            }
            for c in cands
        ]
        payload = {"verdicts": verdicts}
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=300, output_tokens=100),
            parsed_json=payload,
        )


def _install(fake) -> None:
    llm.reset_clients_cache()
    import marginalia.tasks.handlers.vet_relations as vmod
    vmod.get_chat_client = lambda profile="ingest": fake  # type: ignore[assignment]


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed(n_pairs: int) -> list[str]:
    """Seed `n_pairs` fresh (vetted IS NULL) edges with full content."""
    factory = get_session_factory()
    now = _now()
    rel_ids: list[str] = []
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        entries: list[FileEntry] = []
        for i in range(n_pairs * 2):
            f = File(
                id=new_id(),
                storage_key=f"00/aa/x{i}",
                sha256=f"{i:064d}",
                size_bytes=10, mime_type="text/plain",
                original_ext=".txt",
                kind="text", summary=f"Summary of entry {i}.",
                description={"sections": []},
                extra=None, ingest_status="done", ingested_at=now,
                created_at=now, updated_at=now,
            )
            s.add(f); await s.flush()
            e = FileEntry(
                id=new_id(), folder_id=folder.id, file_id=f.id,
                display_name=f"E{i}.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=now, updated_at=now,
            )
            s.add(e); entries.append(e)
        await s.flush()

        for i in range(n_pairs):
            ea, eb = entries[2 * i], entries[2 * i + 1]
            a_id, b_id = sorted((ea.id, eb.id))
            rid = new_id()
            s.add(EntryRelation(
                id=rid,
                entry_a_id=a_id, entry_b_id=b_id,
                note=f"pair-{i}",
                source_kind="mine_session_cooccurrence",
                last_observed_at=now,
                observation_count=5,
                vetted=None, vetted_observation_count=None,
                vetted_at=None,
                created_at=now,
            ))
            rel_ids.append(rid)
        await s.commit()
    return rel_ids


async def _latest_outcome() -> TaskOutcome | None:
    factory = get_session_factory()
    async with factory() as s:
        rows = (
            await s.execute(
                select(TaskOutcome)
                .where(TaskOutcome.task_kind == KIND_VET_RELATIONS)
                .order_by(TaskOutcome.completed_at.desc())
                .limit(1)
            )
        ).scalars().all()
        return rows[0] if rows else None


async def _scenario_total_failure() -> None:
    print("--- scenario: total failure ---")
    _install(_AlwaysFailIngest())
    rel_ids = await _seed(n_pairs=3)

    raised = False
    try:
        await handle_vet_relations({})
    except RuntimeError as exc:
        raised = True
        assert "no verdicts" in str(exc).lower(), f"unexpected msg: {exc}"
        assert "simulated upstream" in str(exc), \
            f"err msg should embed last_error; got: {exc}"
    assert raised, "handler must raise on total failure (was previously silent)"
    print("[1] handle_vet_relations raised RuntimeError as expected")

    # Edges remain unvetted: nothing was committed.
    factory = get_session_factory()
    async with factory() as s:
        for rid in rel_ids:
            r = await s.get(EntryRelation, rid)
            assert r is not None and r.vetted is None, \
                f"rel {rid} must remain unvetted on total failure; got {r.vetted}"
    print("[2] no relation rows were vetted")

    # task_outcomes records error with last_error captured.
    out = await _latest_outcome()
    assert out is not None, "task_outcomes row should exist"
    assert out.outcome == "error", \
        f"outcome must be 'error' on total failure; got {out.outcome!r}"
    detail = out.detail or {}
    assert detail.get("failed", 0) > 0
    assert "simulated upstream" in (detail.get("last_error") or "")
    print(f"[3] task_outcomes outcome='error' detail.failed={detail['failed']} "
          f"last_error captured")


async def _scenario_partial_failure() -> None:
    print("--- scenario: partial failure ---")
    fake = _PartialFailIngest()
    _install(fake)
    # 12 pairs → two batches of 10 + 2 (BATCH_SIZE=10).
    rel_ids = await _seed(n_pairs=12)

    # Must NOT raise: the first batch's verdicts are real work.
    await handle_vet_relations({})
    print(f"[4] handle_vet_relations returned without raising "
          f"(LLM calls={fake.calls})")

    factory = get_session_factory()
    async with factory() as s:
        vetted, unvetted = 0, 0
        for rid in rel_ids:
            r = await s.get(EntryRelation, rid)
            assert r is not None
            if r.vetted is True:
                vetted += 1
            elif r.vetted is None:
                unvetted += 1
    assert vetted >= 10, f"first batch should have committed >=10; got {vetted}"
    assert unvetted >= 2, f"second batch should remain unvetted; got {unvetted}"
    print(f"[5] partial commit visible: vetted={vetted} unvetted={unvetted}")

    out = await _latest_outcome()
    assert out is not None
    assert out.outcome == "error", (
        "partial failure must record 'error' so user sees degraded quality; "
        f"got {out.outcome!r}"
    )
    detail = out.detail or {}
    assert detail.get("yes", 0) >= 10
    assert detail.get("failed", 0) >= 2
    assert "simulated upstream" in (detail.get("last_error") or "")
    print(f"[6] task_outcomes outcome='error' yes={detail['yes']} "
          f"failed={detail['failed']}")


async def _main() -> None:
    await _create_schema()
    await _scenario_total_failure()
    # Reset DB between scenarios — task_outcomes ordering matters.
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    await _scenario_partial_failure()
    print("\nALL VET_RELATIONS FAILURE E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
