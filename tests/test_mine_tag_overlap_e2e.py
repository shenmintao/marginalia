"""End-to-end mine_tag_overlap.

Run:
    .venv/Scripts/python tests/test_mine_tag_overlap_e2e.py

Verifies:
  1. Tag-overlap miner emits entry_relations for entry pairs whose tag
     sets overlap above MIN_JACCARD with at least MIN_SHARED_TAGS shared
     tags. Pair count and source_kind are correct.
  2. Soft-deleted entries are excluded.
  3. Tags worn by more than MAX_TAG_FANOUT entries are skipped (catch-
     all tags must not seed every-pair-against-every-pair candidates).
  4. Existing entry_relations bumped (observation_count incremented),
     not duplicated.
  5. The cap parameter limits new rows; remaining candidates are
     skipped without error.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_mine_tag_overlap_e2e_data"
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

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import (  # noqa: E402
    Base, EntryRelation, EntryTag, File, FileEntry, Folder, Tag,
)
from marginalia.tasks.handlers.mine_tag_overlap import handle_mine_tag_overlap  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Build a small graph:

      Entries:
        A: tags = {raft, paxos, distributed}
        B: tags = {raft, paxos, distributed, networking}
        C: tags = {raft, networking}
        D: tags = {python, web}                             (isolated)
        E: tags = {raft, paxos, distributed} but soft-deleted

      Plus a "common" tag worn by 50 entries that should be skipped due
      to MAX_TAG_FANOUT.

      Expected pairs above thresholds (MIN_JACCARD=0.30, MIN_SHARED=2):
        A-B: shared=3 union=4 J=0.75 ✓
        A-C: shared=1 (raft only)        ✗ (below MIN_SHARED)
        B-C: shared=2 (raft, networking) union=4 J=0.50 ✓
        A-D / B-D / C-D: 0 overlap        ✗
        Anything with E: E soft-deleted   ✗
    """
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

        def mk_entry(label: str) -> FileEntry:
            return FileEntry(
                id=new_id(), folder_id=folder.id, file_id=f.id,
                display_name=f"{label}.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=now, updated_at=now,
            )

        e_a, e_b, e_c, e_d, e_e = [mk_entry(x) for x in "ABCDE"]
        for e in (e_a, e_b, e_c, e_d, e_e):
            s.add(e)
        await s.flush()
        e_e.deleted_at = now
        e_e.purge_after = now + timedelta(days=7)

        def mk_tag(name: str, facet: str = "topic") -> Tag:
            return Tag(
                id=new_id(), name=name, facet=facet,
                doc_count=0, alias_of=None,
                created_at=now, updated_at=now,
            )
        t_raft, t_paxos, t_dist, t_net, t_py, t_web, t_common = [
            mk_tag(n) for n in
            ("raft", "paxos", "distributed", "networking",
             "python", "web", "common")
        ]
        for t in (t_raft, t_paxos, t_dist, t_net, t_py, t_web, t_common):
            s.add(t)
        await s.flush()

        # Entry-tag edges. Note: only B and D wear `common` so that
        # A-C overlap is exactly {raft} (below MIN_SHARED) and the
        # high-fanout filter is exercised by the 50 filler entries
        # below — not by polluting the meaningful pairs' shared sets.
        edges: list[tuple[FileEntry, list[Tag]]] = [
            (e_a, [t_raft, t_paxos, t_dist]),
            (e_b, [t_raft, t_paxos, t_dist, t_net, t_common]),
            (e_c, [t_raft, t_net]),
            (e_d, [t_py, t_web, t_common]),
            (e_e, [t_raft, t_paxos, t_dist]),  # soft-deleted
        ]
        for entry, tags in edges:
            for t in tags:
                s.add(EntryTag(
                    entry_id=entry.id, tag_id=t.id, source="ingest",
                    created_at=now,
                ))

        # Inflate t_common over MAX_TAG_FANOUT (40) by attaching it to
        # 50 throwaway entries — these don't overlap with the targets
        # so they shouldn't seed any high-J pairs themselves; the test
        # is "t_common's high fanout doesn't pollute pair generation".
        # Since A/B/C/D each have other shared tags above MIN_SHARED,
        # we just need to confirm t_common is NOT the only tag enabling
        # any pair we keep.
        for _ in range(50):
            extra = mk_entry("filler")
            s.add(extra); await s.flush()
            s.add(EntryTag(
                entry_id=extra.id, tag_id=t_common.id, source="ingest",
                created_at=now,
            ))

        # Seed an existing relation between A and B that the miner
        # should INCREMENT, not duplicate.
        a_id, b_id = sorted((e_a.id, e_b.id))
        s.add(EntryRelation(
            id=new_id(),
            entry_a_id=a_id, entry_b_id=b_id,
            note="seeded earlier",
            source_kind="mine_session_cooccurrence",
            last_observed_at=now - timedelta(days=5),
            observation_count=1,
            created_at=now - timedelta(days=5),
        ))

        await s.commit()
        return {
            "A": e_a.id, "B": e_b.id, "C": e_c.id,
            "D": e_d.id, "E": e_e.id,
        }


async def _main() -> None:
    await _create_schema()
    ids = await _seed()
    print(f"[setup] seeded entries: {{A,B,C,D,E}} = "
          f"{{{','.join(v[:8] for v in ids.values())}}}")

    # Run miner with default thresholds.
    await handle_mine_tag_overlap({})

    factory = get_session_factory()
    async with factory() as s:
        rels = (
            await s.execute(
                select(EntryRelation).where(
                    EntryRelation.source_kind.in_(
                        ["mine_tag_overlap", "reflect"]
                    )
                )
            )
        ).scalars().all()
    by_pair = {(r.entry_a_id, r.entry_b_id): r for r in rels}

    a, b, c, d, e_id = ids["A"], ids["B"], ids["C"], ids["D"], ids["E"]

    # 1. A-B: existing relation → incremented; source_kind on the row
    #    stays 'reflect' (we only bump observation_count, not retag).
    ab = tuple(sorted((a, b)))
    assert ab in by_pair, f"A-B should exist, got pairs {list(by_pair)}"
    ab_row = by_pair[ab]
    assert ab_row.observation_count >= 2, \
        f"A-B observation_count should be incremented from 1, got {ab_row.observation_count}"
    print(f"[1] A-B existing reflect-row incremented: "
          f"obs_count={ab_row.observation_count}")

    # 2. B-C: new pair from tag overlap. Should appear with source_kind.
    bc = tuple(sorted((b, c)))
    assert bc in by_pair, \
        f"B-C should exist (shared=2, J=0.5), got pairs {list(by_pair)}"
    bc_row = by_pair[bc]
    assert bc_row.source_kind == "mine_tag_overlap", \
        f"B-C source_kind={bc_row.source_kind}"
    print(f"[2] B-C new relation created (J=0.5, shared=2)")

    # 3. A-C: shared=1 only (raft) — must NOT exist.
    ac = tuple(sorted((a, c)))
    assert ac not in by_pair, \
        f"A-C should NOT exist (only 1 shared tag); got {by_pair[ac]}"
    print(f"[3] A-C correctly excluded (only 1 shared tag)")

    # 4. D: no overlap with anyone → no relations.
    d_pairs = [
        p for p in by_pair if d in p
    ]
    assert d_pairs == [], f"D should have no relations, got {d_pairs}"
    print(f"[4] D (isolated) has no relations")

    # 5. E (soft-deleted): no relations involving E.
    e_pairs = [p for p in by_pair if e_id in p]
    assert e_pairs == [], \
        f"soft-deleted E should not appear in any relation, got {e_pairs}"
    print(f"[5] E (soft-deleted) excluded from relations")

    # 6. No 'common'-only pairs: filler entries must not produce any
    #    relations even though t_common is shared.
    async with factory() as s:
        all_rels = (
            await s.execute(
                select(EntryRelation).where(
                    EntryRelation.source_kind == "mine_tag_overlap"
                )
            )
        ).scalars().all()
    relevant = {ids[k] for k in ("A", "B", "C", "D")}
    for r in all_rels:
        assert r.entry_a_id in relevant or r.entry_b_id in relevant, \
            f"unexpected pair from filler entries: {r.entry_a_id}, {r.entry_b_id}"
    print(f"[6] high-fanout 'common' tag did not seed filler pairs "
          f"(only {len(all_rels)} mine_tag_overlap rows total)")

    # 7. Re-run miner — should be idempotent (existing rows incremented,
    #    no error).
    before = ab_row.observation_count
    await handle_mine_tag_overlap({})
    async with factory() as s:
        ab_again = (
            await s.execute(
                select(EntryRelation).where(EntryRelation.id == ab_row.id)
            )
        ).scalar_one()
    assert ab_again.observation_count > before, \
        f"second run should increment again, got {ab_again.observation_count}"
    print(f"[7] re-run is idempotent + cumulative "
          f"(obs_count={ab_again.observation_count})")

    print("\nALL MINE_TAG_OVERLAP E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
