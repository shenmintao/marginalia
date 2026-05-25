"""End-to-end propose_views (Cycle 24).

Run:
    .venv/Scripts/python tests/test_propose_views_e2e.py

Verifies:
  1. Eligible tag clusters with ≥3 tags shared by ≥10 entries are
     surfaced as candidates.
  2. Clusters already covered by an existing view (≥80% tag overlap)
     are excluded from candidates.
  3. Fake LLM accepts some clusters and rejects others.
  4. Accept → INSERT view with name + summary + filter_spec.tags_all
     + audit `view_created` event.
  5. Reject → no view inserted; task_outcomes records the reason.
  6. Re-running the handler → already-evaluated cluster_ids are
     skipped (no LLM call for them).
  7. cap parameter caps new views per run.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_propose_views_e2e_data"
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

from marginalia import llm
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, EntryTag, File, FileEntry, Folder, Tag, View,
)
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.utils.ids import new_id


CALL_LOG: list[ChatRequest] = []


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_fake(plan: dict[str, dict]):
    """plan: cluster_id -> {decision, reason, name?, summary?, filter_tag_ids?}."""
    class _Fake:
        profile_name = "ingest"
        model = "fake-ingest"

        async def complete(self, request: ChatRequest) -> ChatResponse:
            CALL_LOG.append(request)
            ut = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
            ctx_start = ut.index("<clusters>") + len("<clusters>")
            ctx_end = ut.index("</clusters>")
            payload = json.loads(ut[ctx_start:ctx_end].strip())
            decisions = []
            for cl in payload["clusters"]:
                cid = cl["cluster_id"]
                spec = plan.get(cid, {"decision": "reject",
                                      "reason": "default reject"})
                d = {"cluster_id": cid,
                     "decision": spec["decision"],
                     "reason": spec["reason"]}
                if spec["decision"] == "accept":
                    d["name"] = spec.get("name", "Unnamed view")
                    d["summary"] = spec.get("summary", "")
                    d["filter_tag_ids"] = spec.get(
                        "filter_tag_ids", cl["tag_ids"])
                decisions.append(d)
            out = {"decisions": decisions}
            return ChatResponse(
                text=json.dumps(out),
                tool_calls=[],
                stop_reason="end_turn",
                usage=TokenUsage(input_tokens=600, output_tokens=200,
                                 cache_read_tokens=400),
                parsed_json=out,
            )
    return _Fake()


def _install_fake(client):
    llm.reset_clients_cache()
    import marginalia.tasks.handlers.propose_views as mod
    mod.get_chat_client = lambda profile="ingest": client  # type: ignore[assignment]


async def _seed():
    """Seed:
      Cluster A (eligible, expected accept): tags consensus + distributed +
        algorithm shared by 12 entries
      Cluster B (eligible, expected reject): tags todo + draft + 2024 shared
        by 11 entries — too generic
      Cluster C (eligible but already covered): tags graph + node + edge
        shared by 10 entries; pre-existing view covers all three.
    """
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="root",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        # 6 distinct canonical tags
        tag_specs = [
            ("consensus", "topic"),
            ("distributed", "topic"),
            ("algorithm", "topic"),
            ("todo", "extra"),
            ("draft", "extra"),
            ("2024", "time"),
            ("graph", "topic"),
            ("node", "topic"),
            ("edge", "topic"),
        ]
        tags: dict[str, str] = {}  # name -> id
        for name, facet in tag_specs:
            t = Tag(id=new_id(), name=name, facet=facet, alias_of=None,
                    doc_count=0, last_used_at=now,
                    created_at=now, updated_at=now)
            s.add(t)
            tags[name] = t.id
        await s.flush()

        # Helper to make an entry with given tag names
        async def _mk_entry(name: str, tag_names: list[str]) -> FileEntry:
            file_id = new_id()
            f = File(id=file_id, storage_key=f"00/aa/{file_id}",
                     sha256=file_id.replace("-", "") + ("0" * 32),
                     size_bytes=10, mime_type="text/plain",
                     original_ext=".txt", kind="text",
                     summary=f"summary of {name}",
                     description={"sections": []},
                     extra=None, ingest_status="done", ingested_at=now,
                     created_at=now, updated_at=now)
            s.add(f); await s.flush()
            e = FileEntry(id=new_id(), folder_id=folder.id, file_id=f.id,
                          display_name=name, lifecycle="active",
                          catalog_id=None, extra=None,
                          created_at=now, updated_at=now)
            s.add(e); await s.flush()
            for tname in tag_names:
                s.add(EntryTag(entry_id=e.id, tag_id=tags[tname],
                               source="ingest", created_at=now))
            return e

        # Cluster A: 12 entries with consensus + distributed + algorithm
        for i in range(12):
            await _mk_entry(f"a-{i}.md",
                            ["consensus", "distributed", "algorithm"])

        # Cluster B: 11 entries with todo + draft + 2024
        for i in range(11):
            await _mk_entry(f"b-{i}.md", ["todo", "draft", "2024"])

        # Cluster C: 10 entries with graph + node + edge
        for i in range(10):
            await _mk_entry(f"c-{i}.md", ["graph", "node", "edge"])

        # Pre-existing view that covers Cluster C
        s.add(View(
            id=new_id(),
            name="Graph theory",
            summary="Pre-seeded — should block cluster C.",
            description=None, extra=None, tags=["graph", "node", "edge"],
            filter_spec={
                "tags_all": [tags["graph"], tags["node"], tags["edge"]],
                "lifecycle": ["active", "manual_active"],
            },
            created_at=now, updated_at=now,
        ))
        await s.commit()
        return tags


def _cluster_id(tag_ids: list[str]) -> str:
    import hashlib
    return hashlib.sha1("|".join(sorted(tag_ids)).encode()).hexdigest()[:24]


async def main():
    await _create_schema()
    tags = await _seed()
    factory = get_session_factory()

    cluster_a_id = _cluster_id([
        tags["algorithm"], tags["consensus"], tags["distributed"],
    ])
    cluster_b_id = _cluster_id([tags["2024"], tags["draft"], tags["todo"]])

    plan = {
        cluster_a_id: {
            "decision": "accept",
            "reason": "Genuine technical topic — consensus + distributed + algorithm.",
            "name": "Distributed Consensus Algorithms",
            "summary": "Entries discussing consensus mechanisms in distributed systems.",
            "filter_tag_ids": [tags["consensus"], tags["distributed"],
                               tags["algorithm"]],
        },
        cluster_b_id: {
            "decision": "reject",
            "reason": "Generic metadata tags, not a topic.",
        },
    }
    fake = _make_fake(plan)
    _install_fake(fake)

    from marginalia.tasks.handlers.propose_views import handle_propose_views

    # ---- 1. first run --------------------------------------------------
    await handle_propose_views({})

    async with factory() as s:
        # 1.a Accept cluster A → view created
        views = (await s.execute(
            select(View).where(View.name.like("Distributed%"))
        )).scalars().all()
        assert len(views) == 1, f"expected 1 new view, got {len(views)}"
        v = views[0]
        assert v.name == "Distributed Consensus Algorithms"
        assert v.summary and "consensus" in v.summary.lower()
        spec = v.filter_spec or {}
        assert set(spec["tags_all"]) == {
            tags["consensus"], tags["distributed"], tags["algorithm"],
        }
        print(f"[1] view created: name={v.name!r}, "
              f"tags_all_count={len(spec['tags_all'])}")

        # 1.b Cluster B (rejected) — no view
        rejected_view = (await s.execute(
            select(View).where(View.name.like("%2024%"))
        )).scalar_one_or_none()
        assert rejected_view is None
        print("[2] cluster B (todo/draft/2024) rejected — no view")

        # 1.c Cluster C (covered) — was never even sent to LLM
        # Verify by examining LLM call payload
        assert len(CALL_LOG) == 1
        ut = CALL_LOG[0].messages[0].content[0].text  # type: ignore[index, attr-defined]
        assert "graph" not in ut, "covered cluster should not be sent to LLM"
        assert "node" not in ut
        print("[3] cluster C (graph/node/edge) excluded — already covered")

        # 1.d audit
        kinds = (await s.execute(text(
            "SELECT kind, COUNT(*) FROM audit_events "
            "WHERE kind = 'view_created' GROUP BY kind"
        ))).all()
        assert kinds == [("view_created", 1)], f"audit: {kinds}"
        print("[4] audit view_created x 1")

        # 1.e task_outcomes
        outcomes = (await s.execute(text(
            "SELECT object_kind, outcome, COUNT(*) FROM task_outcomes "
            "WHERE task_kind='propose_views' GROUP BY object_kind, outcome"
        ))).all()
        breakdown = {(ok, o): c for ok, o, c in outcomes}
        print(f"[5] outcomes breakdown: {breakdown}")
        assert breakdown.get(("view_proposal", "applied")) == 1
        assert breakdown.get(("view_proposal", "rejected")) == 1
        assert breakdown.get(("global", "applied")) == 1

    # ---- 2. re-run: should not re-evaluate already-evaluated clusters ----
    CALL_LOG.clear()
    await handle_propose_views({})
    print(f"[6] re-run: LLM calls = {len(CALL_LOG)} (0 expected)")
    assert len(CALL_LOG) == 0

    async with factory() as s:
        n_views = (await s.execute(
            text("SELECT COUNT(*) FROM views WHERE name LIKE 'Distributed%'")
        )).scalar()
        assert n_views == 1, f"re-run duplicated view: {n_views}"

    # ---- 3. cap test: more eligible clusters than cap allows -------------
    # Add 3 fresh independent clusters (each 10 entries)
    async with factory() as s:
        # New tag sets — entirely disjoint from existing ones
        new_tag_specs = [
            ("ml", "topic"), ("transformer", "topic"), ("attention", "topic"),
            ("python", "language"), ("fastapi", "topic"), ("rest", "topic"),
            ("devops", "topic"), ("docker", "topic"), ("kubernetes", "topic"),
        ]
        new_tags: dict[str, str] = {}
        for name, facet in new_tag_specs:
            t = Tag(id=new_id(), name=name, facet=facet, alias_of=None,
                    doc_count=0, last_used_at=_now(),
                    created_at=_now(), updated_at=_now())
            s.add(t)
            new_tags[name] = t.id
        await s.flush()
        folder_id = (await s.execute(
            select(Folder.id).limit(1))).scalar_one()
        for prefix, tnames in [
            ("ml-", ["ml", "transformer", "attention"]),
            ("py-", ["python", "fastapi", "rest"]),
            ("ops-", ["devops", "docker", "kubernetes"]),
        ]:
            for i in range(10):
                file_id = new_id()
                f = File(id=file_id, storage_key=f"00/aa/{file_id}",
                         sha256=file_id.replace("-", "") + ("0" * 32),
                         size_bytes=10, mime_type="text/plain",
                         original_ext=".txt", kind="text",
                         summary=f"summary {prefix}{i}",
                         description={"sections": []},
                         extra=None, ingest_status="done", ingested_at=_now(),
                         created_at=_now(), updated_at=_now())
                s.add(f); await s.flush()
                e = FileEntry(id=new_id(), folder_id=folder_id, file_id=f.id,
                              display_name=f"{prefix}{i}.md",
                              lifecycle="active",
                              catalog_id=None, extra=None,
                              created_at=_now(), updated_at=_now())
                s.add(e); await s.flush()
                for tname in tnames:
                    s.add(EntryTag(entry_id=e.id, tag_id=new_tags[tname],
                                   source="ingest", created_at=_now()))
        await s.commit()

    # 3 new clusters all accept
    new_plan = {}
    for tnames in [
        ["ml", "transformer", "attention"],
        ["python", "fastapi", "rest"],
        ["devops", "docker", "kubernetes"],
    ]:
        cid = _cluster_id([new_tags[n] for n in tnames])
        new_plan[cid] = {
            "decision": "accept",
            "reason": f"Topic: {' '.join(tnames)}",
            "name": " ".join(tnames).title(),
            "summary": " + ".join(tnames),
            "filter_tag_ids": [new_tags[n] for n in tnames],
        }
    _install_fake(_make_fake(new_plan))
    CALL_LOG.clear()

    await handle_propose_views({"cap": 1})
    async with factory() as s:
        # Started with 2 views (graph + Distributed) and capped at 1 new
        n_views_now = (await s.execute(
            text("SELECT COUNT(*) FROM views"))).scalar()
        assert n_views_now == 3, f"cap=1 violated: total views={n_views_now}"
        print(f"[7] cap=1: total views now {n_views_now} (started 2)")
        # The 2 over-cap clusters should still be marked as rejected/applied
        # in task_outcomes — but the handler currently only writes outcome
        # for the first-cap successful one and then keeps rejecting. Let's
        # check that all 3 cluster_ids are in task_outcomes one way or other:
        ids_seen = (await s.execute(text(
            "SELECT DISTINCT object_id FROM task_outcomes "
            "WHERE task_kind='propose_views' AND object_kind='view_proposal'"
        ))).scalars().all()
        # First-run had 2 (cluster A + B). Second-run had cap=1 so only the
        # FIRST of 3 new clusters writes a view + applied; the other 2 are
        # still recorded as rejected (because accept-cap was hit).
        # Total distinct cluster_ids: 2 (first) + 3 (second) = 5
        print(f"[7] distinct cluster_ids in task_outcomes: {len(ids_seen)}")
        assert len(ids_seen) >= 5, f"expected ≥5, got {len(ids_seen)}"

    print("\nALL PROPOSE_VIEWS E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
