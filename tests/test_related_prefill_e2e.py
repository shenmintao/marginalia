"""End-to-end related_entries pre-fill in search + get_metadata.

Run:
    .venv/Scripts/python tests/test_related_prefill_e2e.py

The discovery layer's whole point is cutting agent loop count: once an
agent has identified one relevant entry, the next entries it might want
are already attached to the result it has, so it doesn't need a second
search round-trip. This test verifies that pre-fill actually works at
the two surfaces agents and the CLI hit:

  search_entries        each result row carries `related_entries`,
                        top-3, vetted-only.
  get_user_metadata     single-entry detail carries `related_entries`,
                        top-8, vetted-only.

Scenarios:
  1. Search hits A → result has B, C, D in related_entries (vetted).
  2. Search hit's related_entries excludes E (vetted=False).
  3. Search hit's related_entries excludes G (soft-deleted endpoint).
  4. get_user_metadata for A returns same set with up to 8 entries.
  5. Entry with no relations gets related_entries=[].
  6. Result shape matches contract (entry_id, display_name, score).
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_related_prefill_e2e_data"
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
from marginalia.services.user_files import (  # noqa: E402
    get_user_metadata, search_entries,
)
from marginalia.utils.ids import new_id  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    """Build:
      A (raft) — strong vetted edge to B, C, D
                 vetted=False edge to E (rejected by gate)
                 unvetted edge to F (still in candidate pool)
                 vetted=True edge to G but G is soft-deleted
      H — isolated, no relations
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        def mk_file(label: str) -> File:
            return File(
                id=new_id(),
                storage_key=f"00/aa/{label}",
                sha256=label * 16,
                size_bytes=10, mime_type="text/plain",
                original_ext=".txt",
                kind="text", summary=f"summary for {label}",
                description={"sections": []},
                extra=None, ingest_status="done", ingested_at=now,
                created_at=now, updated_at=now,
            )

        files = {label: mk_file(label) for label in "abcdefgh"}
        for f in files.values():
            s.add(f)
        await s.flush()

        def mk_entry(label: str) -> FileEntry:
            return FileEntry(
                id=new_id(), folder_id=folder.id,
                file_id=files[label.lower()].id,
                display_name=f"{label}_paper.txt", lifecycle="active",
                catalog_id=None, extra=None,
                created_at=now, updated_at=now,
            )
        entries = {label: mk_entry(label) for label in "ABCDEFGH"}
        for e in entries.values():
            s.add(e)
        await s.flush()
        # Soft-delete G.
        entries["G"].deleted_at = now
        entries["G"].purge_after = now + timedelta(days=7)

        def mk_rel(
            la: str, lb: str, *, count: int,
            vetted: bool | None = True,
        ):
            ea, eb = entries[la], entries[lb]
            a_id, b_id = sorted((ea.id, eb.id))
            s.add(EntryRelation(
                id=new_id(),
                entry_a_id=a_id, entry_b_id=b_id,
                note=f"{la}-{lb}",
                source_kind="mine_session_cooccurrence",
                last_observed_at=now,
                observation_count=count,
                vetted=vetted,
                vetted_reason="seeded for prefill test" if vetted is not None else None,
                vetted_at=now if vetted is not None else None,
                vetted_observation_count=count if vetted is not None else None,
                created_at=now,
            ))

        mk_rel("A", "B", count=10, vetted=True)
        mk_rel("A", "C", count=8, vetted=True)
        mk_rel("A", "D", count=5, vetted=True)
        mk_rel("A", "E", count=4, vetted=False)
        mk_rel("A", "F", count=3, vetted=None)
        mk_rel("A", "G", count=6, vetted=True)
        # H has no edges.

        await s.commit()
        return {label: e.id for label, e in entries.items()}


async def _main() -> None:
    await _create_schema()
    ids = await _seed()
    print(f"[setup] seeded 8 entries; G soft-deleted, "
          f"vetted=True for B/C/D/G, vetted=False for E, "
          f"vetted=None for F")

    factory = get_session_factory()

    # 1-3. search → A's hit carries related_entries with B/C/D, no E/F/G.
    async with factory() as s:
        results = await search_entries(s, query="A_paper")
    a_row = next((r for r in results if r["entry_id"] == ids["A"]), None)
    assert a_row is not None, "search should find A_paper"
    related = a_row.get("related_entries", [])
    related_ids = {r["entry_id"] for r in related}
    print(f"[1] search A_paper → related_entries: "
          f"{[(r['entry_id'][:6], round(r['score'], 3)) for r in related]}")
    assert ids["B"] in related_ids, f"B (vetted=True) should be in related"
    assert ids["C"] in related_ids, f"C (vetted=True) should be in related"
    assert ids["D"] in related_ids, f"D (vetted=True) should be in related"
    print(f"[2] B/C/D all present (vetted=True)")
    assert ids["E"] not in related_ids, \
        f"E (vetted=False) must NOT appear in related_entries"
    assert ids["F"] not in related_ids, \
        f"F (vetted=None, unvetted) must NOT appear in related_entries"
    assert ids["G"] not in related_ids, \
        f"G (soft-deleted) must NOT appear in related_entries"
    print(f"[3] E/F/G correctly excluded")

    # 4. search has SEARCH_RELATED_TOP_K=3 cap — exactly 3 here.
    assert len(related) <= 3, f"search top_k=3, got {len(related)}"
    print(f"[4] search related_entries respects top_k=3 (got {len(related)})")

    # 5. get_user_metadata returns top-8 (we only have 3 vetted neighbours;
    #    so just verify shape and that all 3 are there).
    async with factory() as s:
        meta = await get_user_metadata(s, entry_id=ids["A"])
    meta_related = meta.get("related_entries", [])
    meta_ids = {r["entry_id"] for r in meta_related}
    assert {ids["B"], ids["C"], ids["D"]}.issubset(meta_ids), \
        f"metadata should surface all 3 vetted neighbours; got {meta_ids}"
    assert ids["E"] not in meta_ids and ids["F"] not in meta_ids
    assert ids["G"] not in meta_ids
    print(f"[5] get_user_metadata related_entries: 3 vetted neighbours, "
          f"unvetted/rejected/dead excluded")

    # 6. Entry with no relations → empty list.
    async with factory() as s:
        h_meta = await get_user_metadata(s, entry_id=ids["H"])
    assert h_meta.get("related_entries") == [], \
        f"H has no relations; expected [], got {h_meta.get('related_entries')}"
    print(f"[6] H (no relations) returns related_entries=[]")

    # 7. Result shape matches contract.
    if related:
        sample = related[0]
        assert {"entry_id", "display_name", "score"}.issubset(sample.keys()), \
            f"shape wrong: {sample}"
        print(f"[7] result shape OK: {sorted(sample.keys())}")

    print("\nALL RELATED_PREFILL E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
