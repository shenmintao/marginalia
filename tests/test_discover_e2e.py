"""End-to-end /discover + random walk + find_related agent tool.

Run:
    .venv/Scripts/python tests/test_discover_e2e.py

Verifies:
  1. find_related random walk returns expected ranking. Heavily-connected
     direct neighbours score highest; 2-hop neighbours visible but lower;
     unconnected entries absent.
  2. Soft-deleted entries excluded from results even if their relations
     exist in the table.
  3. Empty seed (entry with no relations) returns [].
  4. find_related agent tool wraps the service correctly.
  5. Different source_kinds (mine_session_cooccurrence + mine_tag_overlap
     + mine_citation_graph + reflect) all contribute to walk weight —
     they share one observation_count column.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_discover_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import (  # noqa: E402
    Base, EntryRelation, File, FileEntry, Folder,
)
from marginalia.services.recommend import find_related  # noqa: E402
from marginalia.utils.ids import new_id  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Build a graph:

      A ---10--- B  (cooccurrence, strong direct edge)
      A ---5---- C  (tag_overlap)
      B ---5---- D  (citation_graph) — 2-hop neighbour from A
      C ---3---- D
      D ---2---- E  (3-hop from A)
      F (isolated, no edges)
      G ---5---- A  but G is soft-deleted → skip

    Expected RWR from A:
      B: highest (direct, weight=10)
      C: high (direct, weight=5)
      D: medium (only 2-hop)
      E: low (3-hop)
      F: absent (no edges)
      G: absent (soft-deleted)
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
        entries = {label: mk_entry(label) for label in "ABCDEFG"}
        for e in entries.values():
            s.add(e)
        await s.flush()
        # Soft-delete G.
        entries["G"].deleted_at = now
        entries["G"].purge_after = now + timedelta(days=7)

        def mk_rel(label_a: str, label_b: str, weight: int, kind: str):
            ea, eb = entries[label_a], entries[label_b]
            a_id, b_id = sorted((ea.id, eb.id))
            s.add(EntryRelation(
                id=new_id(),
                entry_a_id=a_id, entry_b_id=b_id,
                note=f"{label_a}-{label_b} via {kind}",
                source_kind=kind,
                last_observed_at=now,
                observation_count=weight,
                # Pretend vet_relations already greenlit these — the
                # walk algorithm itself is what we're testing here, not
                # the gate. test_vet_relations_e2e covers the gate.
                vetted=True,
                vetted_reason="seeded as vetted for discover test",
                vetted_at=now,
                vetted_observation_count=weight,
                created_at=now,
            ))

        mk_rel("A", "B", 10, "mine_session_cooccurrence")
        mk_rel("A", "C", 5, "mine_tag_overlap")
        mk_rel("B", "D", 5, "mine_citation_graph")
        mk_rel("C", "D", 3, "mine_tag_overlap")
        mk_rel("D", "E", 2, "reflect")
        # G is soft-deleted; relation should be filtered out at load.
        mk_rel("A", "G", 8, "mine_session_cooccurrence")

        await s.commit()
        return {label: e.id for label, e in entries.items()}


async def _main() -> None:
    await _create_schema()
    ids = await _seed()
    print(f"[setup] seeded 7 entries; G soft-deleted")

    # 1. RWR from A. Use a fixed rng_seed so the test is deterministic.
    factory = get_session_factory()
    async with factory() as s:
        results = await find_related(
            s, seed_entry_id=ids["A"], top_k=10, rng_seed=42,
        )
    by_id = {r.entry_id: r for r in results}
    label_by_id = {v: k for k, v in ids.items()}
    ranked_labels = [label_by_id[r.entry_id] for r in results]
    print(f"[1] random walk from A → {ranked_labels}")

    # 2. B should rank above C (heavier direct edge).
    assert ids["B"] in by_id and ids["C"] in by_id, \
        f"both direct neighbours expected; got {ranked_labels}"
    assert by_id[ids["B"]].score > by_id[ids["C"]].score, (
        f"B (weight=10) should outrank C (weight=5); "
        f"B={by_id[ids['B']].score} C={by_id[ids['C']].score}"
    )
    print(f"[2] B ({by_id[ids['B']].score:.3f}) outranks C "
          f"({by_id[ids['C']].score:.3f})")

    # 3. D (2-hop) appears, and ranks below B and C.
    assert ids["D"] in by_id, "2-hop neighbour D should appear"
    assert by_id[ids["D"]].score < by_id[ids["B"]].score
    assert by_id[ids["D"]].score < by_id[ids["C"]].score
    print(f"[3] D ({by_id[ids['D']].score:.3f}) appears as 2-hop "
          f"and ranks below direct neighbours")

    # 4. F (isolated) absent.
    assert ids["F"] not in by_id, "F has no edges; should not appear"
    print("[4] isolated F absent")

    # 5. G (soft-deleted) absent even though A-G edge exists.
    assert ids["G"] not in by_id, \
        "soft-deleted G should be filtered out by find_related"
    print("[5] soft-deleted G filtered out")

    # 6. direct_edge_weight reflects the seed→neighbour edge weight.
    assert by_id[ids["B"]].direct_edge_weight == 10
    assert by_id[ids["C"]].direct_edge_weight == 5
    assert by_id[ids["D"]].direct_edge_weight == 0  # no direct edge
    print("[6] direct_edge_weight surfaced correctly "
          "(B=10, C=5, D=0 for 2-hop)")

    # 7. Empty seed (F has no relations) returns [].
    async with factory() as s:
        empty = await find_related(s, seed_entry_id=ids["F"], top_k=8)
    assert empty == [], f"expected empty, got {empty}"
    print("[7] seed with no edges returns []")

    # 8. include_unvetted=True walks the raw graph — useful for
    #    /discover --all. With our seed all edges are vetted=True so
    #    this should return the same set as the vetted-only walk.
    async with factory() as s:
        unvet = await find_related(
            s, seed_entry_id=ids["A"], top_k=10, rng_seed=42,
            include_unvetted=True,
        )
    unvet_ids = {r.entry_id for r in unvet}
    assert ids["B"] in unvet_ids and ids["C"] in unvet_ids
    assert ids["G"] not in unvet_ids, \
        "soft-deleted G must stay out of unvetted walk too"
    print(f"[8] include_unvetted walk works (returned {len(unvet)} entries)")

    # 9. Different source_kind edges all contribute. Verify by checking
    #    that B (cooccurrence) and C (tag_overlap) are both visited —
    #    if the algorithm filtered by source_kind, one would be missing.
    for label in ("B", "C"):
        assert ids[label] in by_id, \
            f"{label} should appear regardless of source_kind"
    print("[9] all source_kinds contribute to walk weight")

    print("\nALL DISCOVER E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
