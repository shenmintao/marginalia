"""End-to-end vet_relations.

Run:
    .venv/Scripts/python tests/test_vet_relations_e2e.py

Verifies the LLM-gated relation vetting pipeline:

  1. Fresh edges (vetted IS NULL) are picked up. LLM verdict is stored
     verbatim into vetted / vetted_reason / vetted_at /
     vetted_observation_count.
  2. Yes/No verdicts are honoured: vetted=True for "yes",
     vetted=False for "no".
  3. Edges with observation_count below MIN_OBSERVATION_TO_VET are
     skipped.
  4. Soft-deleted endpoint pairs are skipped.
  5. Edges without summary on either side are skipped (LLM has nothing
     to judge).
  6. Re-running with no growth + no TTL expiry is a no-op (vetted=True
     stays vetted).
  7. observation_count growing past 2*snapshot+5 triggers re-vet.
  8. find_related (default vetted-only) returns vetted=True neighbours
     and excludes vetted=False ones.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_vet_relations_e2e_data"
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
    Base, EntryRelation, File, FileEntry, Folder,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.services.recommend import find_related  # noqa: E402
from marginalia.tasks.handlers.vet_relations import (  # noqa: E402
    handle_vet_relations,
)
from marginalia.utils.ids import new_id  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


PLAN: dict[tuple[str, str], dict[str, str]] = {}


class _FakeVetIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        ut = request.messages[0].content[0].text  # type: ignore[attr-defined, index]
        cs = ut.index("<candidates>") + len("<candidates>")
        ce = ut.index("</candidates>")
        cands = json.loads(ut[cs:ce].strip())["candidates"]
        verdicts = []
        for c in cands:
            spec = PLAN.get(
                tuple(sorted((c["a"]["display_name"], c["b"]["display_name"]))),
                {"verdict": "no", "reason": "default reject"},
            )
            verdicts.append({
                "pair_id": c["pair_id"],
                "verdict": spec["verdict"],
                "reason": spec["reason"],
            })
        payload = {"verdicts": verdicts}
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=300, output_tokens=100),
            parsed_json=payload,
        )


def _install_fake() -> None:
    llm.reset_clients_cache()
    fake = _FakeVetIngest()
    import marginalia.tasks.handlers.vet_relations as vmod
    vmod.get_chat_client = lambda profile="ingest": fake  # type: ignore[assignment]


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        def mk_file(label: str, summary: str | None) -> File:
            f = File(
                id=new_id(),
                storage_key=f"00/aa/{label}",
                sha256=label * 10 + "z" * (64 - len(label) * 10),
                size_bytes=10, mime_type="text/plain",
                original_ext=".txt",
                kind="text", summary=summary,
                description={"sections": []},
                extra=None, ingest_status="done", ingested_at=now,
                created_at=now, updated_at=now,
            )
            s.add(f)
            return f

        f_a = mk_file("a", "Raft consensus algorithm primer.")
        f_b = mk_file("b", "Multi-Paxos in practice.")
        f_c = mk_file("c", "Cookbook: chocolate chip cookies.")
        f_d = mk_file("d", "Distributed locks and fencing tokens.")
        f_e = mk_file("e", "Soft-deleted entry's file.")
        f_no_sum = mk_file("nosum", None)  # missing summary
        await s.flush()

        def mk_entry(label: str, file_id: str) -> FileEntry:
            return FileEntry(
                id=new_id(), folder_id=folder.id, file_id=file_id,
                display_name=f"{label}.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=now, updated_at=now,
            )
        e_a = mk_entry("A_raft", f_a.id)
        e_b = mk_entry("B_paxos", f_b.id)
        e_c = mk_entry("C_cookies", f_c.id)
        e_d = mk_entry("D_locks", f_d.id)
        e_e = mk_entry("E_dead", f_e.id)
        e_n = mk_entry("N_nosum", f_no_sum.id)
        for e in (e_a, e_b, e_c, e_d, e_e, e_n):
            s.add(e)
        await s.flush()
        e_e.deleted_at = now
        e_e.purge_after = now + timedelta(days=7)

        def mk_rel(
            ea: FileEntry, eb: FileEntry, *,
            count: int, vetted: bool | None = None,
            vetted_obs: int | None = None,
            vetted_at: datetime | None = None,
        ) -> str:
            a_id, b_id = sorted((ea.id, eb.id))
            rid = new_id()
            s.add(EntryRelation(
                id=rid,
                entry_a_id=a_id, entry_b_id=b_id,
                note=f"{ea.display_name}↔{eb.display_name}",
                source_kind="mine_session_cooccurrence",
                last_observed_at=now,
                observation_count=count,
                vetted=vetted, vetted_observation_count=vetted_obs,
                vetted_at=vetted_at,
                created_at=now,
            ))
            return rid

        # AB: fresh, count=5, plan accept
        ab = mk_rel(e_a, e_b, count=5)
        # AC: fresh, count=4, plan reject (raft vs cookies — clearly unrelated)
        ac = mk_rel(e_a, e_c, count=4)
        # AD: below threshold (count=1)
        ad = mk_rel(e_a, e_d, count=1)
        # AE: count=4 but E soft-deleted → skip
        ae = mk_rel(e_a, e_e, count=4)
        # AN: count=4 but N has no summary → skip
        an = mk_rel(e_a, e_n, count=4)
        # BD: already vetted=True with snapshot=4, count=4, no growth → skip on rerun
        bd = mk_rel(
            e_b, e_d, count=4,
            vetted=True, vetted_obs=4, vetted_at=now,
        )

        await s.commit()
        return {
            "A": e_a.id, "B": e_b.id, "C": e_c.id,
            "D": e_d.id, "E": e_e.id, "N": e_n.id,
            "rel_AB": ab, "rel_AC": ac, "rel_AD": ad,
            "rel_AE": ae, "rel_AN": an, "rel_BD": bd,
        }


async def _main() -> None:
    _install_fake()
    await _create_schema()
    ids = await _seed()

    PLAN[tuple(sorted(("A_raft.txt", "B_paxos.txt")))] = {
        "verdict": "yes",
        "reason": "Both consensus algorithms; same domain.",
    }
    PLAN[tuple(sorted(("A_raft.txt", "C_cookies.txt")))] = {
        "verdict": "no",
        "reason": "Different topics (consensus vs cooking).",
    }

    await handle_vet_relations({})

    factory = get_session_factory()
    async with factory() as s:
        rels_by_id: dict[str, EntryRelation] = {}
        for rid in (
            ids["rel_AB"], ids["rel_AC"], ids["rel_AD"],
            ids["rel_AE"], ids["rel_AN"], ids["rel_BD"],
        ):
            r = await s.get(EntryRelation, rid)
            assert r is not None
            rels_by_id[rid] = r

    # 1. AB: fresh + LLM=yes → vetted=True with reason + snapshot
    ab = rels_by_id[ids["rel_AB"]]
    assert ab.vetted is True, f"AB should be vetted=True; got {ab.vetted}"
    assert "consensus" in (ab.vetted_reason or "").lower()
    assert ab.vetted_observation_count == 5
    assert ab.vetted_at is not None
    print(f"[1] AB vetted=True with snapshot={ab.vetted_observation_count}, "
          f"reason='{ab.vetted_reason[:40]}...'")

    # 2. AC: fresh + LLM=no → vetted=False
    ac = rels_by_id[ids["rel_AC"]]
    assert ac.vetted is False, f"AC should be vetted=False; got {ac.vetted}"
    assert ac.vetted_observation_count == 4
    print(f"[2] AC vetted=False (rejected as unrelated)")

    # 3. AD: count=1 below MIN_OBSERVATION_TO_VET → not touched
    ad = rels_by_id[ids["rel_AD"]]
    assert ad.vetted is None, f"AD count=1 below threshold; should be untouched"
    print(f"[3] AD count=1 skipped (below MIN_OBSERVATION_TO_VET)")

    # 4. AE: E soft-deleted → not touched
    ae = rels_by_id[ids["rel_AE"]]
    assert ae.vetted is None, "AE endpoint soft-deleted; should be untouched"
    print(f"[4] AE skipped (endpoint soft-deleted)")

    # 5. AN: N has no summary → not touched
    an = rels_by_id[ids["rel_AN"]]
    assert an.vetted is None, "AN endpoint missing summary; should be untouched"
    print(f"[5] AN skipped (endpoint missing summary)")

    # 6. BD: already vetted=True with snapshot=count → no re-vet
    bd = rels_by_id[ids["rel_BD"]]
    assert bd.vetted is True
    print(f"[6] BD already-vetted with no growth → skipped re-vet")

    # 7. Bump BD's count past growth threshold → next run re-vets.
    async with factory() as s:
        bd_live = await s.get(EntryRelation, ids["rel_BD"])
        bd_live.observation_count = bd_live.vetted_observation_count * 3 + 6
        await s.commit()
    PLAN[tuple(sorted(("B_paxos.txt", "D_locks.txt")))] = {
        "verdict": "yes",
        "reason": "Both about distributed coordination primitives.",
    }
    await handle_vet_relations({})
    async with factory() as s:
        bd_after = await s.get(EntryRelation, ids["rel_BD"])
    assert bd_after.vetted_observation_count > 4, \
        f"BD should be re-vetted with new snapshot; got {bd_after.vetted_observation_count}"
    print(f"[7] BD re-vetted after growth: snapshot now {bd_after.vetted_observation_count}")

    # 8. find_related (vetted-only) sees AB but not AC. Walk from A.
    async with factory() as s:
        rows = await find_related(
            s, seed_entry_id=ids["A"], top_k=5, rng_seed=7,
        )
    visit_ids = {r.entry_id for r in rows}
    assert ids["B"] in visit_ids, "B (vetted=True) should appear"
    assert ids["C"] not in visit_ids, \
        "C (vetted=False) must NOT appear in default walk"
    print(f"[8] find_related shows B (vetted=True), hides C (vetted=False)")

    print("\nALL VET_RELATIONS E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
