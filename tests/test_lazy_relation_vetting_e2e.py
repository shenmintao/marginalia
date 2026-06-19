from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from marginalia.config import get_settings
from marginalia.db.engine import dispose_engine, get_engine
from marginalia.db.models import Base, EntryRelation, File, FileEntry, Folder, Task
from marginalia.db.models.task_outcomes import TaskOutcome
from marginalia.db.session import session_scope
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.tasks.handlers.periodic_tick import handle_periodic_tick
from marginalia.tasks.kinds import KIND_PERIODIC_TICK, KIND_VET_RELATIONS
from marginalia.utils.ids import new_id


class _FakeOnDemandVet:
    profile_name = "ingest"
    model = "fake-ingest"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_batches: list[list[str]] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.calls += 1
        text = _request_text(request)
        start = text.index("<candidates>") + len("<candidates>")
        end = text.index("</candidates>")
        candidates = json.loads(text[start:end].strip())["candidates"]
        self.seen_batches.append([c["pair_id"] for c in candidates])
        lines: list[str] = []
        for candidate in candidates:
            names = {
                candidate["a"]["display_name"],
                candidate["b"]["display_name"],
            }
            if names == {"A_raft.txt", "B_paxos.txt"}:
                verdict = "yes"
                reason = "Both discuss distributed consensus."
            else:
                verdict = "no"
                reason = "Different subject matter."
            lines.append(f"{candidate['pair_id']}: {verdict} - {reason}")
        return ChatResponse(
            text="<verdicts>\n" + "\n".join(lines) + "\n</verdicts>",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=300, output_tokens=80),
            parsed_json=None,
        )


def _request_text(request: ChatRequest) -> str:
    parts: list[str] = []
    for msg in request.messages:
        if isinstance(msg.content, str):
            parts.append(msg.content)
        else:
            parts.extend(getattr(block, "text", "") for block in msg.content)
    return "\n".join(parts)


async def _prepare_home(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("MARGINALIA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("LLM_DEFAULT_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "fake-model")
    monkeypatch.setenv("RELATION_BACKGROUND_VETTING_ENABLED", "false")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    await dispose_engine()
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_unvetted_graph() -> dict[str, str]:
    now = datetime.now(timezone.utc)
    async with session_scope() as db:
        folder = Folder(
            id=new_id(),
            parent_id=None,
            name="root",
            created_at=now,
            updated_at=now,
        )
        db.add(folder)
        await db.flush()

        def make_file(label: str, summary: str) -> File:
            row = File(
                id=new_id(),
                storage_key=f"00/aa/{label}",
                sha256=(label * 64)[:64],
                size_bytes=10,
                mime_type="text/plain",
                original_ext=".txt",
                kind="text",
                summary=summary,
                description={"sections": []},
                extra=None,
                ingest_status="done",
                ingested_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            return row

        f_a = make_file("a", "Raft consensus overview.")
        f_b = make_file("b", "Paxos and consensus systems.")
        f_c = make_file("c", "Chocolate chip cookie recipe.")
        await db.flush()

        def make_entry(label: str, file_id: str) -> FileEntry:
            row = FileEntry(
                id=new_id(),
                folder_id=folder.id,
                file_id=file_id,
                display_name=f"{label}.txt",
                lifecycle="active",
                catalog_id=None,
                extra=None,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            return row

        e_a = make_entry("A_raft", f_a.id)
        e_b = make_entry("B_paxos", f_b.id)
        e_c = make_entry("C_cookies", f_c.id)
        await db.flush()

        def make_relation(left: FileEntry, right: FileEntry, count: int) -> str:
            a_id, b_id = sorted((left.id, right.id))
            relation_id = new_id()
            db.add(EntryRelation(
                id=relation_id,
                entry_a_id=a_id,
                entry_b_id=b_id,
                note=f"{left.display_name} / {right.display_name}",
                source_kind="mine_session_cooccurrence",
                last_observed_at=now,
                observation_count=count,
                vetted=None,
                vetted_reason=None,
                vetted_at=None,
                vetted_observation_count=None,
                created_at=now,
            ))
            return relation_id

        rel_ab = make_relation(e_a, e_b, 5)
        rel_ac = make_relation(e_a, e_c, 4)
        await db.commit()
        return {
            "A": e_a.id,
            "B": e_b.id,
            "C": e_c.id,
            "rel_AB": rel_ab,
            "rel_AC": rel_ac,
        }


@pytest.mark.asyncio
async def test_discover_explicit_vet_queues_background_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await _prepare_home(monkeypatch, tmp_path)
    fake = _FakeOnDemandVet()
    import marginalia.tasks.handlers.vet_relations as vet_mod

    vet_mod.get_chat_client = lambda profile="ingest": fake  # type: ignore[assignment]
    ids = await _seed_unvetted_graph()

    from marginalia.main import app

    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            first = await client.get(f"/v1/discover/{ids['A']}", params={"top_k": 5})
            assert first.status_code == 200, first.text
            first_body = first.json()
            assert first_body["results"] == []
            assert first_body["vetting"] is None
            assert fake.calls == 0

            queued = await client.get(
                f"/v1/discover/{ids['A']}",
                params={"top_k": 5, "vet": "true"},
            )
            assert queued.status_code == 200, queued.text
            queued_body = queued.json()
            assert queued_body["results"] == []
            assert queued_body["vetting"]["candidates_available"] is True
            assert queued_body["vetting"]["queued"] is True
            task_id = queued_body["vetting"]["task_id"]
            assert task_id
            assert fake.calls == 0

    from marginalia.tasks.handlers.vet_relations import handle_vet_relations

    async with session_scope() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        assert task.kind == KIND_VET_RELATIONS
        assert task.payload["entry_id"] == ids["A"]
        payload = dict(task.payload)

    await handle_vet_relations(payload)
    assert fake.calls == 1

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            second = await client.get(f"/v1/discover/{ids['A']}", params={"top_k": 5})
            assert second.status_code == 200, second.text
            second_results = second.json()["results"]
            assert [row["entry_id"] for row in second_results] == [ids["B"]]

    async with session_scope() as db:
        ab = await db.get(EntryRelation, ids["rel_AB"])
        ac = await db.get(EntryRelation, ids["rel_AC"])
        assert ab is not None and ab.vetted is True
        assert ab.vetted_observation_count == 5
        assert "consensus" in (ab.vetted_reason or "").lower()
        assert ac is not None and ac.vetted is False
        assert ac.vetted_observation_count == 4


@pytest.mark.asyncio
async def test_explicit_vetting_skips_enqueue_when_seed_has_no_raw_edges(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await _prepare_home(monkeypatch, tmp_path)
    from marginalia.repositories import entry_relations as relations_repo
    from marginalia.services.relation_vetting import schedule_direct_relation_vetting

    async def _fail_detail_query(*_args, **_kwargs):
        raise AssertionError("candidate detail query should be skipped")

    monkeypatch.setattr(
        relations_repo,
        "list_direct_unvetted_candidates",
        _fail_detail_query,
    )

    async with session_scope() as db:
        result = await schedule_direct_relation_vetting(
            db,
            entry_id="entry-with-no-relations",
            limit=5,
        )
        vet_tasks = (
            await db.execute(select(Task.id).where(Task.kind == KIND_VET_RELATIONS))
        ).scalars().all()

    assert result.requested is True
    assert result.candidates_available is False
    assert result.queued is False
    assert vet_tasks == []


@pytest.mark.asyncio
async def test_periodic_does_not_enqueue_background_relation_vetting_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    await _prepare_home(monkeypatch, tmp_path)
    await handle_periodic_tick({})

    async with session_scope() as db:
        vet_tasks = (
            await db.execute(select(Task.id).where(Task.kind == KIND_VET_RELATIONS))
        ).scalars().all()
        assert vet_tasks == []
        tick_detail = (
            await db.execute(
                select(TaskOutcome.detail)
                .where(TaskOutcome.task_kind == KIND_PERIODIC_TICK)
                .order_by(TaskOutcome.completed_at.desc())
            )
        ).scalars().first()
        assert tick_detail is not None
        assert KIND_VET_RELATIONS in tick_detail["skipped_disabled"]
