"""End-to-end check for the 7 new agent tools (Cycle 11b).

Run:
    .venv/Scripts/python tests/test_agent_tools_e2e.py

This drives each tool directly (no LLM stub) — we synthesise a small but
realistic dataset and assert each tool returns the expected shape.

Tools covered:
  1. list_catalogs (root + child)
  2. read_catalog (full detail + children + entries)
  3. resolve_tag (direct + alias_of + tag_aliases fallback + facet filter)
  4. materialize_view (catalog_subtree + tags_all + lifecycle)
  5. search_metadata (text OR array + tags_all + catalog_subtree + lifecycle + view_id)
  6. read_entries_metadata (full hydration + related_entries)
  7. read_files (section_id / heading / line_start-line_end / offset+max_chars / pattern)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_agent_tools_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.agent.tools import ToolContext, get_tool
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Catalog, EntryRelation, EntryTag, File, FileEntry, Folder,
    Tag, TagAlias, View,
)
from marginalia.storage import get_storage
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    factory = get_session_factory()
    storage = get_storage()
    now = _now()
    body = (
        "# Overview\n\nThis is the overview section.\n\n"
        "# Pipeline\n\nDetails of the pipeline.\n\n"
        "# Closing\n\nFinal remarks about consensus.\n"
    ).encode("utf-8")

    async def _stream():
        yield body

    storage_key = "00/aa/test-paper"
    await storage.put(storage_key, _stream(), content_type="text/markdown")

    async with factory() as s:
        # folders
        f_root = Folder(id=new_id(), parent_id=None, name="Research",
                        created_at=now, updated_at=now)
        s.add(f_root)
        await s.flush()

        # catalog tree: Research -> {LLM, DB}
        c_research = Catalog(id=new_id(), parent_id=None, name="Research",
                             summary="Top-level research", description=None,
                             extra=None, tags=["topic:research"],
                             created_at=now, updated_at=now)
        s.add(c_research)
        await s.flush()
        c_llm = Catalog(id=new_id(), parent_id=c_research.id, name="LLM",
                        summary="Language model research", description=None,
                        extra="Active area", tags=["topic:llm"],
                        created_at=now, updated_at=now)
        c_db = Catalog(id=new_id(), parent_id=c_research.id, name="DB",
                       summary="Database research", description=None,
                       extra=None, tags=["topic:database"],
                       created_at=now, updated_at=now)
        s.add_all([c_llm, c_db])
        await s.flush()

        # files + entries
        f_paper = File(id=new_id(), storage_key=storage_key,
                       sha256="z" * 64, size_bytes=len(body),
                       mime_type="text/markdown", original_ext=".md", kind="text",
                       summary="Paper on consensus algorithms",
                       description={
                           "sections": [
                               {"id": "s1", "title": "Overview",
                                "anchor": {"unit": "lines", "value": "1-3"},
                                "summary": "Intro", "key_terms": ["consensus"]},
                               {"id": "s2", "title": "Pipeline",
                                "anchor": {"unit": "lines", "value": "5-7"},
                                "summary": "Pipeline detail",
                                "key_terms": ["raft", "paxos"]},
                           ],
                           "coverage": {
                               "unit": "pages",
                               "total_pages": 12,
                               "indexed_pages": 5,
                               "indexed_partial": True,
                               "partial_reasons": ["text_page_cap"],
                               "max_index_pages": 5,
                           },
                       },
                       extra="Cross-cutting note about consensus.",
                       ingest_status="done", ingested_at=now,
                       created_at=now, updated_at=now)
        s.add(f_paper)
        await s.flush()

        e_a = FileEntry(id=new_id(), folder_id=f_root.id, file_id=f_paper.id,
                        display_name="paper-a.md", lifecycle="active",
                        catalog_id=c_llm.id, extra="Position note A",
                        created_at=now, updated_at=now)
        e_b = FileEntry(id=new_id(), folder_id=f_root.id, file_id=f_paper.id,
                        display_name="paper-b.md", lifecycle="active",
                        catalog_id=c_db.id, extra=None,
                        created_at=now, updated_at=now)
        e_archived = FileEntry(id=new_id(), folder_id=f_root.id, file_id=f_paper.id,
                               display_name="old-paper.md",
                               lifecycle="archived",
                               catalog_id=c_llm.id, extra=None,
                               created_at=now, updated_at=now)
        s.add_all([e_a, e_b, e_archived])
        await s.flush()

        # tags + alias
        t_consensus = Tag(id=new_id(), name="consensus", facet="topic",
                          alias_of=None, doc_count=2, last_used_at=now,
                          created_at=now, updated_at=now)
        t_raft = Tag(id=new_id(), name="raft", facet="topic",
                     alias_of=None, doc_count=1, last_used_at=now,
                     created_at=now, updated_at=now)
        t_paxos_alias = Tag(id=new_id(), name="paxos", facet="topic",
                            alias_of=None, doc_count=0, last_used_at=now,
                            created_at=now, updated_at=now)
        s.add_all([t_consensus, t_raft, t_paxos_alias])
        await s.flush()
        # an alias TAG row pointing at consensus
        t_distconsensus = Tag(id=new_id(), name="distributed-consensus", facet="topic",
                              alias_of=t_consensus.id, doc_count=0, last_used_at=now,
                              created_at=now, updated_at=now)
        s.add(t_distconsensus)
        # a tag_aliases history row from old name 'concensus' -> consensus
        s.add(TagAlias(id=new_id(), from_name="concensus",
                       to_tag_id=t_consensus.id, note=None, created_at=now))
        await s.flush()

        # entry_tags
        s.add_all([
            EntryTag(entry_id=e_a.id, tag_id=t_consensus.id,
                     source="ingest", created_at=now),
            EntryTag(entry_id=e_a.id, tag_id=t_raft.id,
                     source="ingest", created_at=now),
            EntryTag(entry_id=e_b.id, tag_id=t_consensus.id,
                     source="ingest", created_at=now),
        ])

        # entry_relation between e_a and e_b (canonical: a < b lex order)
        a, b = sorted((e_a.id, e_b.id))
        s.add(EntryRelation(
            id=new_id(), entry_a_id=a, entry_b_id=b,
            note="Both papers compare consensus algorithms",
            source_kind="mine_session_cooccurrence", last_observed_at=now,
            observation_count=3, created_at=now,
        ))

        # a view: filter_spec = entries under Research subtree with tag consensus
        v = View(id=new_id(), name="Consensus reading list",
                 summary="Active papers about consensus.", description=None,
                 extra=None, tags=None,
                 filter_spec={
                     "catalog_subtree": [c_research.id],
                     "tags_all": [t_consensus.id],
                     "lifecycle": ["active", "manual_active"],
                 },
                 created_at=now, updated_at=now)
        s.add(v)

        await s.commit()
        return {
            "folder_root": f_root.id,
            "c_research": c_research.id, "c_llm": c_llm.id, "c_db": c_db.id,
            "e_a": e_a.id, "e_b": e_b.id, "e_archived": e_archived.id,
            "t_consensus": t_consensus.id, "t_raft": t_raft.id,
            "t_distconsensus": t_distconsensus.id,
            "view_id": v.id,
            "file_id": f_paper.id,
        }


async def _call(name: str, args: dict, session_id="s", conv_id="c") -> dict:
    factory = get_session_factory()
    reg = get_tool(name)
    assert reg is not None, f"tool {name} not registered"
    ctx = ToolContext(session_id=session_id, conversation_id=conv_id)
    async with factory() as s:
        result = await reg.handler(s, ctx, args)
        await s.commit()
    return result


async def main():
    await _create_schema()
    seeded = await _seed()

    # ---- 1. list_catalogs --------------------------------------------------
    roots = await _call("list_catalogs", {"parent_id": None})
    print("[1] list_catalogs(root):",
          [(c["name"], c["doc_count"]) for c in roots["catalogs"]])
    names = {c["name"] for c in roots["catalogs"]}
    assert names == {"Research"}, names
    roots_omitted = await _call("list_catalogs", {})
    assert {c["name"] for c in roots_omitted["catalogs"]} == {"Research"}
    roots_string_null = await _call("list_catalogs", {"parent_id": "null"})
    assert {c["name"] for c in roots_string_null["catalogs"]} == {"Research"}
    children = await _call("list_catalogs", {"parent_id": seeded["c_research"]})
    child_names = {c["name"] for c in children["catalogs"]}
    print("[1] children of Research:", child_names)
    assert child_names == {"LLM", "DB"}

    # ---- 2. read_catalog ---------------------------------------------------
    rc = await _call("read_catalog", {"id": seeded["c_llm"], "entries_limit": 10})
    print("[2] read_catalog(LLM): entries=",
          [e["display_name"] for e in rc["entries"]])
    assert rc["name"] == "LLM"
    assert rc["extra"] == "Active area"
    # only the live entries (paper-a) link directly to LLM; archived also
    # links to LLM but lifecycle filter? read_catalog includes all live entries
    entry_names = {e["display_name"] for e in rc["entries"]}
    # archived is NOT excluded by read_catalog (it's deleted_at IS NULL only)
    assert "paper-a.md" in entry_names

    # ---- 3. resolve_tag ----------------------------------------------------
    r = await _call("resolve_tag", {"name": "consensus"})
    assert r["found"] and r["id"] == seeded["t_consensus"]
    assert r["was_alias"] is False

    r2 = await _call("resolve_tag", {"name": "distributed-consensus"})
    print("[3] resolve_tag('distributed-consensus'):", r2["id"], "was_alias=", r2["was_alias"])
    assert r2["found"] and r2["id"] == seeded["t_consensus"]
    assert r2["was_alias"] is True

    r3 = await _call("resolve_tag", {"name": "concensus"})  # via tag_aliases
    print("[3] resolve_tag('concensus' via aliases):", r3["via"])
    assert r3["found"] and r3["id"] == seeded["t_consensus"]
    assert r3["via"] == "tag_aliases"

    r4 = await _call("resolve_tag", {"name": "no-such-tag"})
    assert r4["found"] is False

    # ---- 4. materialize_view ----------------------------------------------
    mv = await _call("materialize_view", {"id": seeded["view_id"]})
    print("[4] materialize_view:",
          [e["display_name"] for e in mv["entries"]])
    mv_names = {e["display_name"] for e in mv["entries"]}
    # Both paper-a and paper-b are under Research subtree AND have consensus tag.
    # paper-archived is archived → excluded by lifecycle filter.
    assert mv_names == {"paper-a.md", "paper-b.md"}, mv_names

    # ---- 5. search_metadata ------------------------------------------------
    sm = await _call("search_metadata", {
        "text": "consensus",
        "tags_all": [seeded["t_consensus"]],
        "catalog_subtree": seeded["c_research"],
        "limit": 50,
    })
    print("[5] search_metadata:",
          [e["display_name"] for e in sm["entries"]])
    sm_names = {e["display_name"] for e in sm["entries"]}
    assert sm_names == {"paper-a.md", "paper-b.md"}, sm_names
    sm_coverage = {e["display_name"]: e.get("coverage") for e in sm["entries"]}
    assert sm_coverage["paper-a.md"]["indexed_partial"] is True, sm_coverage
    assert "text_page_cap" in sm_coverage["paper-b.md"]["partial_reasons"], sm_coverage

    sm_or = await _call("search_metadata", {
        "text": ["paper-a", "paper-b"],
        "limit": 50,
    })
    sm_or_names = {e["display_name"] for e in sm_or["entries"]}
    assert sm_or_names == {"paper-a.md", "paper-b.md"}, sm_or_names

    sm_split = await _call("search_metadata", {
        "text": "paper-a paper-b",
        "limit": 50,
    })
    sm_split_names = {e["display_name"] for e in sm_split["entries"]}
    assert sm_split_names == {"paper-a.md", "paper-b.md"}, sm_split_names

    # search_metadata with mutually-exclusive args → error
    bad = await _call("search_metadata", {
        "catalog_id": seeded["c_research"],
        "catalog_subtree": seeded["c_research"],
    })
    assert "error" in bad and "mutually exclusive" in bad["error"], bad

    # search via view_id intersection
    sm_v = await _call("search_metadata", {
        "view_id": seeded["view_id"],
        "tags_all": [seeded["t_raft"]],
    })
    sm_v_names = {e["display_name"] for e in sm_v["entries"]}
    print("[5] search_metadata via view+tag:", sm_v_names)
    assert sm_v_names == {"paper-a.md"}, sm_v_names

    # ---- 6. read_entries_metadata -----------------------------------------
    rem = await _call("read_entries_metadata", {
        "entry_ids": [seeded["e_a"], seeded["e_b"]],
        "related_limit": 10,
    })
    assert rem["count"] == 2
    e_a_obj = next(e for e in rem["entries"] if e["entry_id"] == seeded["e_a"])
    print("[6] read_entries_metadata(e_a) catalog_path:",
          [c["name"] for c in e_a_obj["catalog_path"]])
    assert [c["name"] for c in e_a_obj["catalog_path"]] == ["Research", "LLM"]
    assert {t["name"] for t in e_a_obj["tags"]} == {"consensus", "raft"}
    assert len(e_a_obj["related_entries"]) == 1
    assert e_a_obj["related_entries"][0]["entry_id"] == seeded["e_b"]
    assert e_a_obj["related_entries"][0]["observation_count"] == 3

    # ---- 7. read_files -----------------------------------------------------
    rf = await _call("read_files", {
        "requests": [
            {
                "entry_id": seeded["e_a"],
                "reads": [
                    {"section_id": "s1"},
                    {"heading": "Pipeline"},
                    {"line_start": 5, "line_end": 7},
                    {"offset": 0, "max_chars": 25},
                    {"pattern": "consensus", "context_lines": 1, "max_matches": 5},
                ],
            },
        ],
    })
    print("[7] read_files reads:",
          [(r.get("ok"), 'text' in r) for r in rf["results"][0]["reads"]])
    assert rf["count"] == 1
    reads = rf["results"][0]["reads"]
    # section s1 → Overview body (or extras hint when anchor unresolvable)
    section_read = reads[0]
    assert section_read["ok"] is True, section_read
    section_text = section_read.get("text", "")
    section_extras = section_read.get("extras", {})
    assert (
        "Overview" in section_text
        or section_extras.get("title") == "Overview"
    ), section_read
    # heading "Pipeline"
    heading_read = reads[1]
    assert heading_read["ok"] is True, heading_read
    heading_text = heading_read.get("text", "")
    heading_extras = heading_read.get("extras", {})
    assert (
        "Pipeline" in heading_text
        or heading_extras.get("title") == "Pipeline"
    ), heading_read
    # lines 5-7
    lines_read = reads[2]
    assert lines_read["ok"] is True
    assert "Pipeline" in (lines_read.get("text") or ""), lines_read
    # offset/max_chars (the "bytes 0-25" equivalent in the new contract)
    chunk_read = reads[3]
    assert chunk_read["ok"] is True
    assert "Overview" in (chunk_read.get("text") or ""), chunk_read
    # pattern hits 'consensus'
    pattern_read = reads[4]
    assert pattern_read["ok"] is True
    assert pattern_read["extras"]["match_count"] >= 1, pattern_read
    print("[7] pattern match_count:", pattern_read["extras"]["match_count"])

    rf_patterns = await _call("read_files", {
        "requests": [{
            "entry_id": seeded["e_a"][:8],
            "reads": [{
                "patterns": ["consensus", "pipeline"],
                "context_lines": 0,
                "max_matches": 5,
            }],
        }],
    })
    pattern_reads = rf_patterns["results"][0]["reads"]
    assert len(pattern_reads) == 2, pattern_reads
    assert all(item["ok"] is True for item in pattern_reads), pattern_reads
    assert [item["args"]["pattern"] for item in pattern_reads] == [
        "consensus", "pipeline",
    ]
    assert all(item["extras"]["match_count"] >= 1 for item in pattern_reads)
    print("[7b] multi-pattern reads:", [item["args"]["pattern"] for item in pattern_reads])

    # invalid section_id → error
    rf_bad = await _call("read_files", {
        "requests": [{"entry_id": seeded["e_a"], "reads": [
            {"section_id": "no-such-section"},
        ]}],
    })
    bad_read = rf_bad["results"][0]["reads"][0]
    assert bad_read["ok"] is False
    assert "section not found" in bad_read["error"], bad_read

    print("\nALL AGENT_TOOLS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
