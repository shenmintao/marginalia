"""End-to-end normalize_tags sanity check.

Run:
    .venv/Scripts/python tests/test_normalize_tags_e2e.py

Verifies:
  1. Synthesize 6 tags (3 synonym groups across 2 facets):
       topic: 'LLM' / 'large language model' / 'Claude' / 'claude'
       form:  'pdf' / 'PDF'
     and 3 entries with overlapping tag attachments (some entries have BOTH
     the canonical and the to-be-merged tag — this exercises the PK conflict
     path).
  2. Stub LLM to produce two merge groups (one per facet).
  3. Run handler. Verify:
     - tags.alias_of points at canonical (no chained aliases)
     - tag_aliases history rows added for each merged-in NAME
     - entry_tags has been rewritten to canonical (no broken FKs, no PK dupes)
     - doc_count recomputed correctly
     - audit kinds: tag_merged (×3 merges); the run-summary now lives in
       task_outcomes (object_kind='global'), not audit
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_normalize_tags_e2e_data"
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
    Base, EntryTag, File, FileEntry, Folder, Tag, TagAlias,
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


# ---- fake LLM driver --------------------------------------------------------

def _make_fake_llm(merges_by_facet: dict[str, list[dict]]):
    class _FakeChatClient:
        profile_name = "ingest"
        model = "fake-model"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            CALL_LOG.append(request)
            user_text = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
            facet = None
            for f in ("topic", "form", "time", "source", "language", "extra"):
                if f"Facet: {f}" in user_text:
                    facet = f
                    break
            payload = {"merges": merges_by_facet.get(facet or "", [])}
            return ChatResponse(
                text=json.dumps(payload),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=400, output_tokens=80, cache_read_tokens=300),
                parsed_json=payload,
            )

    return _FakeChatClient()


def _install(client) -> None:
    import marginalia.tasks.handlers.normalize_tags as nmod
    nmod.get_chat_client = lambda profile="ingest": client  # type: ignore[assignment]


# ---- seed -------------------------------------------------------------------

async def _seed():
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root", created_at=now, updated_at=now)
        f = File(id=new_id(), storage_key="00/aa/x", sha256="z"*64, size_bytes=10,
                 mime_type="text/plain", original_ext=".txt", kind="text",
                 summary="x", description={"sections": []}, extra=None,
                 ingest_status="done", ingested_at=now,
                 created_at=now, updated_at=now)
        s.add_all([folder, f])
        await s.flush()

        e1 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="a.txt", lifecycle="active",
                       created_at=now, updated_at=now)
        e2 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="b.txt", lifecycle="active",
                       created_at=now, updated_at=now)
        e3 = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                       display_name="c.txt", lifecycle="active",
                       created_at=now, updated_at=now)
        s.add_all([e1, e2, e3])
        await s.flush()

        tags = {}
        for name, facet in [
            ("LLM", "topic"),
            ("large language model", "topic"),
            ("Claude", "topic"),
            ("claude", "topic"),
            ("pdf", "form"),
            ("PDF", "form"),
        ]:
            t = Tag(id=new_id(), name=name, facet=facet, alias_of=None,
                    doc_count=0, last_used_at=now,
                    created_at=now, updated_at=now)
            s.add(t)
            tags[name] = t
        await s.flush()

        # entry_tags attachments designed to exercise PK-collision path:
        # entry e1 has BOTH 'LLM' and 'large language model' → after merge,
        # the PK (e1, LLM_id) and (e1, llm_id) would collide; handler must
        # delete the alias row before update.
        for entry, tag_name in [
            (e1, "LLM"),
            (e1, "large language model"),  # collision target
            (e2, "large language model"),  # plain redirect
            (e2, "Claude"),
            (e3, "claude"),                 # plain redirect for case-merge
            (e3, "pdf"),                    # plain redirect for PDF case-merge
            (e1, "PDF"),                    # canonical already on e1
            (e2, "PDF"),
        ]:
            s.add(EntryTag(entry_id=entry.id, tag_id=tags[tag_name].id,
                           source="ingest", created_at=now))

        # set doc_count by hand so we can verify recompute
        for t in tags.values():
            t.doc_count = 999  # bogus
        await s.commit()

        return {
            "tags": {name: t.id for name, t in tags.items()},
            "entries": {"e1": e1.id, "e2": e2.id, "e3": e3.id},
        }


async def main():
    await _create_schema()
    seeded = await _seed()
    tags = seeded["tags"]
    entries = seeded["entries"]

    fake = _make_fake_llm({
        "topic": [
            {"canonical_id": tags["LLM"], "merge_in_ids": [tags["large language model"]]},
            {"canonical_id": tags["Claude"], "merge_in_ids": [tags["claude"]]},
        ],
        "form": [
            {"canonical_id": tags["PDF"], "merge_in_ids": [tags["pdf"]]},
        ],
    })
    _install(fake)

    from marginalia.tasks.handlers.normalize_tags import handle_normalize_tags
    await handle_normalize_tags({})

    factory = get_session_factory()
    async with factory() as s:
        # alias_of correct
        rows = (await s.execute(select(Tag.name, Tag.alias_of))).all()
        alias_map = {n: a for n, a in rows}
        print("[1] alias_of map:")
        for n, a in alias_map.items():
            label = "canonical" if a is None else f"-> {a}"
            print(f"    {n}: {label}")
        assert alias_map["LLM"] is None
        assert alias_map["large language model"] == tags["LLM"]
        assert alias_map["Claude"] is None
        assert alias_map["claude"] == tags["Claude"]
        assert alias_map["PDF"] is None
        assert alias_map["pdf"] == tags["PDF"]

        # NO chained aliases (alias_of must point at a NULL alias_of)
        canonical_ids = {tid for n, tid in (await s.execute(
            select(Tag.name, Tag.id).where(Tag.alias_of.is_(None))
        )).all() if n in ("LLM", "Claude", "PDF")}
        for n, a in alias_map.items():
            if a is not None:
                assert a in canonical_ids, f"alias {n} points at non-canonical {a}"

        # tag_aliases history rows
        history = (await s.execute(
            select(TagAlias.from_name, TagAlias.to_tag_id)
        )).all()
        print("[2] tag_aliases history:", history)
        history_set = {(n, t) for n, t in history}
        assert ("large language model", tags["LLM"]) in history_set
        assert ("claude", tags["Claude"]) in history_set
        assert ("pdf", tags["PDF"]) in history_set
        assert len(history) == 3

        # entry_tags rewritten correctly. Build (entry, tag_name) view.
        et = (await s.execute(
            select(EntryTag.entry_id, Tag.name)
            .join(Tag, Tag.id == EntryTag.tag_id)
        )).all()
        et_set = {(eid, n) for eid, n in et}
        print("[3] entry_tags after merge:", sorted(et_set))
        # e1 should have LLM (collision-deduped to single row), Claude=no, PDF
        assert (entries["e1"], "LLM") in et_set
        # the duplicate (e1, LLM via merge of large language model) must NOT exist twice
        e1_llm_count = sum(1 for eid, n in et if eid == entries["e1"] and n == "LLM")
        assert e1_llm_count == 1, f"PK dedupe failed: {e1_llm_count}"
        # e2: LLM (redirected), Claude, PDF
        assert (entries["e2"], "LLM") in et_set
        assert (entries["e2"], "Claude") in et_set
        assert (entries["e2"], "PDF") in et_set
        # e3: Claude (redirected), PDF (redirected)
        assert (entries["e3"], "Claude") in et_set
        assert (entries["e3"], "PDF") in et_set
        # No row should still point at any merged tag
        merged_ids = {tags["large language model"], tags["claude"], tags["pdf"]}
        bad = (await s.execute(
            select(EntryTag.tag_id).where(EntryTag.tag_id.in_(merged_ids))
        )).all()
        assert bad == [], f"entry_tags still references merged tags: {bad}"

        # doc_count recomputed: count entry_tag rows per tag id
        counts = {n: c for n, c in (await s.execute(
            select(Tag.name, Tag.doc_count)
        )).all()}
        print("[4] doc_count after recompute:", counts)
        # Each canonical's count must equal the actual entry_tags row count
        canonical_actual = (await s.execute(text(
            "SELECT t.name, COUNT(et.tag_id) FROM tags t "
            "LEFT JOIN entry_tags et ON et.tag_id = t.id "
            "WHERE t.alias_of IS NULL GROUP BY t.name"
        ))).all()
        for name, real in canonical_actual:
            assert counts[name] == real, f"{name}: doc_count={counts[name]} actual={real}"
        # Aliases should all read 0 (no entry_tags points at them)
        for alias_name in ("large language model", "claude", "pdf"):
            assert counts[alias_name] == 0, f"alias {alias_name} doc_count != 0: {counts[alias_name]}"

        # audit + task_outcomes
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events GROUP BY kind"
        ))).all()
        kc = {k: c for k, c in kinds}
        print("[5] audit:", kc)
        assert kc.get("tag_merged") == 3

        outcomes = (await s.execute(text(
            "SELECT outcome FROM task_outcomes WHERE task_kind='normalize_tags'"
        ))).scalars().all()
        print("[5] normalize_tags task_outcomes:", outcomes)
        assert len(outcomes) == 1
        assert outcomes[0] == "applied"

    print("\nALL NORMALIZE_TAGS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
