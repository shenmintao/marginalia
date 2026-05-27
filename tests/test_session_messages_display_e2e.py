"""GET /v1/sessions/{id}/messages emits resolved `display` strings.

The live SSE tool_call event carries a server-resolved `display`
(`list_folder Papers`); the replay payload must mirror that so the
GUI doesn't fall back to printing raw uuids in the chat history.

Asserts:
  1. tool_calls in the replay carry a `display` field per call.
  2. folder_id / parent_id / catalog_id args are resolved to their
     names (not uuids) in `display`.
  3. entry_id args are resolved to display_name (parity with
     test_session_messages_e2e — re-checked here for completeness).

Run:
    .venv/Scripts/python tests/test_session_messages_display_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_session_messages_display_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

import httpx
from httpx import ASGITransport

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Catalog, Conversation, File, FileEntry, Folder, Session,
)
from marginalia.main import app
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed() -> dict:
    factory = get_session_factory()
    now = _now()
    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="Papers")
        s.add(folder); await s.flush()

        catalog = Catalog(
            id=new_id(), parent_id=None, name="Algorithms",
        )
        s.add(catalog); await s.flush()

        f = File(
            id=new_id(), storage_key=f"sk-{new_id()}",
            sha256=("a" * 64), size_bytes=10, ingest_status="done",
        )
        s.add(f); await s.flush()
        entry = FileEntry(
            id=new_id(), folder_id=folder.id, file_id=f.id,
            display_name="raft-paper.pdf", lifecycle="active",
        )
        s.add(entry); await s.flush()

        sess = Session(
            id=new_id(), started_at=now, ended_at=now,
            end_reason="normal", initiating_user_message="hi",
            turn_count=1,
            total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=3,
            total_llm_calls=0, total_duration_ms=0,
        )
        s.add(sess); await s.flush()

        # One conversation with three tool_calls referencing each id type.
        conv = Conversation(
            id=new_id(), session_id=sess.id, turn_index=0,
            started_at=now, ended_at=now,
            user_message="show me raft",
            agent_response="here you go",
            tool_calls=[
                {
                    "name": "list_folder",
                    "arguments": {"parent_id": folder.id},
                    "duration_ms": 1,
                },
                {
                    "name": "read_catalog",
                    "arguments": {"id": catalog.id},
                    "duration_ms": 1,
                },
                {
                    "name": "read_files",
                    "arguments": {
                        "requests": [{"entry_id": entry.id, "reads": []}]
                    },
                    "duration_ms": 1,
                },
            ],
            llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=3, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(conv)
        await s.commit()

        return {
            "session_id": sess.id, "folder_name": folder.name,
            "catalog_name": catalog.name, "entry_name": entry.display_name,
        }


async def test_replay_display_resolves_names() -> None:
    seeded = await _seed()
    transport = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/v1/sessions/{seeded['session_id']}/messages")
            assert r.status_code == 200, r.text
            payload = r.json()
            assert len(payload["turns"]) == 1
            calls = payload["turns"][0]["tool_calls"]
            assert len(calls) == 3, calls

            by_name = {tc["name"]: tc for tc in calls}
            assert "display" in by_name["list_folder"], by_name["list_folder"]
            assert seeded["folder_name"] in by_name["list_folder"]["display"], (
                by_name["list_folder"]["display"], seeded["folder_name"]
            )
            print("[1] list_folder display includes folder name (not uuid)")

            assert seeded["catalog_name"] in by_name["read_catalog"]["display"], (
                by_name["read_catalog"]["display"], seeded["catalog_name"]
            )
            print("[2] read_catalog display includes catalog name")

            assert seeded["entry_name"] in by_name["read_files"]["display"], (
                by_name["read_files"]["display"], seeded["entry_name"]
            )
            print("[3] read_files display includes entry display_name")

            # No raw uuid leaks into any display.
            for tc in calls:
                assert seeded["session_id"][:8] not in tc["display"]
            print("[4] no uuid prefix leaks into display")


async def main() -> None:
    await _create_schema()
    await test_replay_display_resolves_names()
    print("\nALL REPLAY-DISPLAY CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
