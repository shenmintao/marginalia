"""End-to-end conversation export — Cycle 16.

Run:
    .venv/Scripts/python tests/test_export_e2e.py

Verifies:
  1. parse_citations handles `[^a]: entry_id=...` and the new
     `entry_id=..., section_id=..., - reason` shape.
  2. /conversations/{id}/export produces a zip with:
       - report.md      (verbatim agent_response)
       - manifest.json  (citations + missing list)
       - references/<safe>  bytes of each cited live entry
       - references/<safe>.metadata.json  user-visible metadata blob
  3. Soft-deleted entry → recorded as missing, NOT raised
  4. Conversation that hasn't ended → 409
  5. CLI /export works end-to-end and uses ctx.history when no conv given
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_export_e2e_data"
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
from sqlalchemy import select, text

from marginalia.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia.cli import CliContext, MarginaliaClient, dispatch
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import (
    Base, Conversation, File, FileEntry, Folder, Session,
)
from marginalia.main import app
from marginalia.services.exports import parse_citations
from marginalia.storage import get_storage
from marginalia.utils.ids import new_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed():
    factory = get_session_factory()
    storage = get_storage()
    now = _now()

    body_a = b"Paper A on Raft consensus.\n" * 5
    body_b = b"Paper B on Paxos.\n" * 5

    async def _stream(b: bytes):
        async def _it():
            yield b
        return _it()

    await storage.put("00/aa/a", await _stream(body_a), content_type="text/plain")
    await storage.put("00/aa/b", await _stream(body_b), content_type="text/plain")

    async with factory() as s:
        folder = Folder(id=new_id(), parent_id=None, name="research",
                        created_at=now, updated_at=now)
        s.add(folder); await s.flush()

        f_a = File(id=new_id(), storage_key="00/aa/a", sha256="a"*64,
                   size_bytes=len(body_a),
                   mime_type="text/plain", original_ext=".txt", kind="text",
                   summary="Raft note", description={"sections": [
                       {"id": "s2", "title": "Election"}
                   ]},
                   extra=None, ingest_status="done", ingested_at=now,
                   created_at=now, updated_at=now)
        f_b = File(id=new_id(), storage_key="00/aa/b", sha256="b"*64,
                   size_bytes=len(body_b),
                   mime_type="text/plain", original_ext=".txt", kind="text",
                   summary="Paxos note", description={"sections": []},
                   extra=None, ingest_status="done", ingested_at=now,
                   created_at=now, updated_at=now)
        s.add_all([f_a, f_b]); await s.flush()

        e_a = FileEntry(id=new_id(), folder_id=folder.id, file_id=f_a.id,
                        display_name="raft.md", lifecycle="active",
                        catalog_id=None, extra=None,
                        created_at=now, updated_at=now)
        e_b = FileEntry(id=new_id(), folder_id=folder.id, file_id=f_b.id,
                        display_name="paxos.md", lifecycle="active",
                        catalog_id=None, extra=None,
                        created_at=now, updated_at=now)
        # ghost: soft-deleted; agent_response will cite this and we expect
        # it to land in manifest.missing.
        e_ghost = FileEntry(id=new_id(), folder_id=folder.id, file_id=f_b.id,
                            display_name="ghost.md", lifecycle="active",
                            deleted_at=_now(),
                            catalog_id=None, extra=None,
                            created_at=now, updated_at=now)
        s.add_all([e_a, e_b, e_ghost]); await s.flush()

        sess = Session(
            id=new_id(), started_at=now, ended_at=None, end_reason=None,
            initiating_user_message="compare raft and paxos", turn_count=0,
            total_input_tokens=0, total_output_tokens=0, total_cache_read=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add(sess); await s.flush()

        agent_response = (
            "Raft 是 leader-based 共识算法[^a]，"
            "Paxos 则是经典的多数决算法[^b]。\n"
            "另外参考一份历史文档[^g]。\n\n"
            f"[^a]: entry_id={e_a.id}, section_id=s2 - 第二节给出选举流程的伪代码\n"
            f"[^b]: entry_id={e_b.id} - 整篇文档讲 Paxos\n"
            f"[^g]: entry_id={e_ghost.id} - 历史对比，可能已删除\n"
        )
        ended_conv = Conversation(
            id=new_id(), session_id=sess.id, turn_index=0,
            started_at=now, ended_at=_now(),
            user_message="compare raft and paxos",
            agent_response=agent_response,
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        unended_conv = Conversation(
            id=new_id(), session_id=sess.id, turn_index=1,
            started_at=now, ended_at=None,
            user_message="still thinking", agent_response=None,
            tool_calls=[], llm_calls=[],
            total_input_tokens=0, total_output_tokens=0,
            total_tool_calls=0, total_llm_calls=0, total_duration_ms=0,
        )
        s.add_all([ended_conv, unended_conv])
        await s.commit()
        return {
            "session_id": sess.id,
            "conv_done": ended_conv.id,
            "conv_running": unended_conv.id,
            "e_a": e_a.id, "e_b": e_b.id, "e_ghost": e_ghost.id,
            "body_a": body_a, "body_b": body_b,
            "agent_response": agent_response,
        }


async def main():
    # ---- 1. parser unit ------------------------------------------------
    sample = (
        "x[^a] y[^b]\n\n"
        "[^a]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1241, "
        "section_id=s2 - because it covers election\n"
        "[^b]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1242 - whole doc\n"
        "[^c]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1243, "
        "quote=\"first quote\" - quoted doc\n"
        "[^d]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1244, "
        "page=54 - page doc\n"
        "[^e]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1245, "
        "quote=\"no page quote\", page=N/A - no page placeholder\n"
    )
    cites = parse_citations(sample)
    assert len(cites) == 5
    assert cites[0].marker == "a"
    assert cites[0].section_id == "s2"
    assert cites[0].reason and "election" in cites[0].reason
    assert cites[1].section_id is None
    assert cites[1].reason == "whole doc"
    assert cites[2].quote == "first quote"
    assert cites[2].reason == "quoted doc"
    assert cites[3].page == "54"
    assert cites[3].reason == "page doc"
    assert cites[4].quote == "no page quote"
    assert cites[4].page is None
    assert cites[4].reason == "no page placeholder"
    non_contract = (
        "[^x]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1245，page=7 - bad\n"
        "[^y]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1246, "
        "quote=\"first\" + \"second\" - bad\n"
        "[^z]: entry_id=019e5493-fca4-7524-b8d0-3c36885b1247, "
        "page=54（p.54） - bad\n"
    )
    assert parse_citations(non_contract) == []
    print("[1] parse_citations OK")

    # ---- 2. setup + run export endpoint -------------------------------
    await _create_schema()
    seeded = await _seed()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            # ended → 200 zip
            async with c.stream(
                "GET",
                f"/v1/conversations/{seeded['conv_done']}/export",
            ) as r:
                assert r.status_code == 200, r.text
                hdr_count = int(r.headers.get("x-citation-count") or 0)
                hdr_missing = int(r.headers.get("x-missing-count") or 0)
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf.extend(chunk)

            print("[2] export header citation_count =", hdr_count,
                  " missing_count =", hdr_missing)
            assert hdr_count == 3
            assert hdr_missing == 1

            zf = zipfile.ZipFile(io.BytesIO(bytes(buf)), "r")
            names = sorted(zf.namelist())
            print("[2] zip members:", names)

            # report.md verbatim
            report = zf.read("report.md").decode("utf-8")
            assert report == seeded["agent_response"]

            # manifest
            manifest = json.loads(zf.read("manifest.json"))
            print("[2] manifest citation count:", len(manifest["citations"]))
            assert manifest["conversation_id"] == seeded["conv_done"]
            assert len(manifest["citations"]) == 3
            ghost_cite = next(
                c for c in manifest["citations"] if c["entry_id"] == seeded["e_ghost"]
            )
            assert ghost_cite["missing"] is True
            assert seeded["e_ghost"] in manifest["missing"]
            # reasons preserved
            a_cite = next(c for c in manifest["citations"] if c["marker"] == "a")
            assert a_cite["reason"] and "选举" in a_cite["reason"]

            # references include the two live entries' bytes + metadata
            assert any(n == "references/raft.md" for n in names)
            assert any(n == "references/paxos.md" for n in names)
            assert any(n.endswith("raft.md.metadata.json") for n in names)
            assert any(n.endswith("paxos.md.metadata.json") for n in names)
            # ghost MUST NOT have its bytes in the zip
            assert all("ghost" not in n for n in names)

            assert zf.read("references/raft.md") == seeded["body_a"]
            assert zf.read("references/paxos.md") == seeded["body_b"]

            raft_meta = json.loads(zf.read("references/raft.md.metadata.json"))
            assert raft_meta["entry_id"] == seeded["e_a"]
            assert raft_meta["display_name"] == "raft.md"
            assert raft_meta["summary"] == "Raft note"
            # AI-internal fields must NOT leak
            for forbidden in ("catalog_id", "description", "kind", "extra", "tags"):
                assert forbidden not in raft_meta, f"leaked: {forbidden}"
            print("[2] zip contents OK")

            # ---- 3. unended conv → 409 ---------------------------------
            r = await c.get(
                f"/v1/conversations/{seeded['conv_running']}/export"
            )
            assert r.status_code == 409
            print("[3] unended conversation rejected")

            # ---- 4. unknown conv → 404 --------------------------------
            r = await c.get(
                "/v1/conversations/nonexistent-id/export"
            )
            assert r.status_code == 404
            print("[4] unknown conversation 404")

            # ---- 4b. markdown export ----------------------------------
            r = await c.get(
                f"/v1/conversations/{seeded['conv_done']}/export.md"
            )
            assert r.status_code == 200, r.text
            assert r.headers["content-type"].startswith("text/markdown")
            md_body = r.text
            # frontmatter + question header
            assert md_body.startswith("---\n")
            assert "compare raft and paxos" in md_body
            # raw entry_id should be GONE (rewritten to display name)
            assert seeded["e_a"] not in md_body
            assert seeded["e_b"] not in md_body
            # display_name + summary expanded inline
            assert "raft.md" in md_body
            assert "Raft note" in md_body
            assert "paxos.md" in md_body
            # ghost is missing → "(reference removed)" placeholder
            assert "(reference removed)" in md_body
            # body markers untouched
            assert "[^a]" in md_body and "[^b]" in md_body
            print("[4b] markdown export OK")

        # ---- 5. CLI /export ------------------------------------------
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as raw:
            client = MarginaliaClient(base_url="http://t", transport=transport)
            ctx = CliContext(client=client)

            # explicit conv id
            cli_dest = _TEST_ROOT / "via_cli.zip"
            await dispatch(ctx, f"/export {seeded['conv_done']} {cli_dest}")
            assert cli_dest.exists()
            assert cli_dest.stat().st_size > 100
            print("[5] CLI /export with explicit id OK; bytes =",
                  cli_dest.stat().st_size)

            # markdown destination → single .md file, no zip
            md_dest = _TEST_ROOT / "via_cli.md"
            await dispatch(ctx, f"/export {seeded['conv_done']} {md_dest}")
            assert md_dest.exists()
            md_text = md_dest.read_text(encoding="utf-8")
            assert "raft.md" in md_text and "Raft note" in md_text
            assert seeded["e_a"] not in md_text
            print("[5b] CLI /export <id> *.md OK")

            # /export with no id and no history → graceful message
            await dispatch(ctx, "/export")
            print("[5] CLI /export with no id OK (graceful)")

            # populate history; /export with no args uses the last conv
            ctx.history.append({
                "user": "...", "assistant": "...",
                "conversation_id": seeded["conv_done"],
            })
            cli_dest2 = _TEST_ROOT / "from_history.zip"
            # Provide JUST the dest? No — /export's positional is conv_id,
            # so explicit syntax is `/export <id> <dest>`. From-history
            # behaviour is `/export` alone using ctx.history; there's no
            # syntax for "from history but custom dest". Use cwd default.
            old_cwd = os.getcwd()
            os.chdir(_TEST_ROOT)
            try:
                await dispatch(ctx, "/export")
            finally:
                os.chdir(old_cwd)
            default_dest = _TEST_ROOT / f"conversation-{seeded['conv_done'][:8]}.zip"
            assert default_dest.exists(), \
                f"export-from-history did not create {default_dest}"
            print("[5] CLI /export from history OK; default name:",
                  default_dest.name)

            await client.aclose()

    print("\nALL EXPORT E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
