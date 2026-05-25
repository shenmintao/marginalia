"""End-to-end test for actual .rar handling via py7zz / 7zz.

Run:
    .venv/Scripts/python tests/test_rar_e2e.py

Locates a `Rar.exe` (WinRAR's CLI) on the host to build a tiny .rar
fixture — RAR creation is proprietary, no Python library can do it from
scratch. If no Rar.exe is found, the test prints a clear skip notice
and exits 0 so it doesn't break CI hosts without WinRAR installed.

When a fixture is produced, the test verifies that:

  1. ArchivePipeline can walk the .rar via py7zz (the bundled 7zz
     binary's RAR decoder, NOT WinRAR — so this confirms the runtime
     path users will actually hit).
  2. Member listing matches what we put in.
  3. Read-time member dispatch works (text member -> TextPipeline.
     read_segment_from_bytes returns body bytes).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_rar_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"


def _locate_rar_exe() -> str | None:
    """Find a RAR creation CLI. Search PATH first, then common Windows
    install dirs. Returns None if nothing is available — caller skips."""
    found = shutil.which("rar") or shutil.which("Rar") or shutil.which("Rar.exe")
    if found:
        return found
    candidates = [
        r"C:\Program Files\WinRAR\Rar.exe",
        r"C:\Program Files (x86)\WinRAR\Rar.exe",
        "/usr/bin/rar",
        "/opt/homebrew/bin/rar",
    ]
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


RAR_EXE = _locate_rar_exe()
if not RAR_EXE:
    print("[skip] no Rar.exe / rar CLI found; cannot create .rar fixture")
    print("       (install WinRAR or run on a host with `rar` in PATH)")
    sys.exit(0)


def _build_rar_fixture() -> bytes:
    """Use the located rar CLI to build a tiny multi-member .rar. We
    don't ship a pre-built rar in the repo: that would carry attribution
    overhead for what is just a test artefact."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "notes.md").write_text(
            "# Test\n\nThis archive holds two members for the e2e test.\n",
            encoding="utf-8",
        )
        (td / "data.txt").write_text(
            "alpha\nbeta\ngamma\n", encoding="utf-8",
        )
        out = td / "fixture.rar"
        # `a` = add, `-ep1` strips the outer dir name, `-o+` overwrites,
        # `-inul` silences progress output (RAR uses -inul, not -q).
        cmd = [
            RAR_EXE, "a", "-ep1", "-o+", "-inul",
            str(out), str(td / "notes.md"), str(td / "data.txt"),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"rar build failed (exit {result.returncode}): "
                f"{result.stderr or result.stdout}"
            )
        return out.read_bytes()


from sqlalchemy import select  # noqa: E402

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File  # noqa: E402
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.pipelines import resolve_pipeline  # noqa: E402
from marginalia.storage import get_storage, open_archive  # noqa: E402


CALL_LOG: list[ChatRequest] = []


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        payload = {
            "summary": "A small RAR fixture with two members.",
            "description": {
                "archive_kind": "rar",
                "primary_language": None,
                "frameworks_detected": [],
            },
            "kind": "container",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Tests"],
            "entry_tags": [{"name": "rar", "facet": "form"}],
        }
        return ChatResponse(
            text=json.dumps(payload), tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=80),
            parsed_json=payload,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    fake = _FakeIngest()

    def _factory(profile: str = "ingest"):
        return fake

    import marginalia.pipelines.archive as amod
    amod.get_chat_client = _factory  # type: ignore[assignment]


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed(body: bytes, name: str) -> str:
    from marginalia.services.upload import upload
    storage = get_storage()

    async def _stream():
        yield body

    factory = get_session_factory()
    async with factory() as db:
        result = await upload(
            db, storage,
            stream=_stream(), fallback_name=name,
            remote_path=f"/tests/{name}",
            content_type="application/vnd.rar",
        )
        await db.commit()
        return result.file_id


async def _ingest(file_id: str) -> None:
    from marginalia.tasks.handlers.ingest_file import handle_ingest_file
    await handle_ingest_file({"file_id": file_id, "entry_id": None})


# ---- test ------------------------------------------------------------------

async def _main() -> None:
    print(f"[setup] using rar at {RAR_EXE}")
    rar_bytes = _build_rar_fixture()
    print(f"[setup] built fixture.rar: {len(rar_bytes)} bytes; "
          f"magic = {rar_bytes[:7].hex()}")
    # Sanity: every RAR4 archive starts with "Rar!\x1a\x07\x00".
    assert rar_bytes.startswith(b"Rar!"), \
        f"fixture missing RAR magic; got {rar_bytes[:8]!r}"

    # 1. Routing.
    pipe = resolve_pipeline(
        "application/vnd.rar", ".rar", filename="fixture.rar",
    )
    assert pipe is not None and pipe.name == "archive", \
        f"resolver didn't pick archive: {pipe}"
    print("[1] .rar routes to ArchivePipeline")

    # 2. open_archive walks members via py7zz/7zz.
    with open_archive(rar_bytes, "fixture.rar") as session:
        names = sorted(m.path for m in session.members)
        assert names == ["data.txt", "notes.md"], \
            f"unexpected member list: {names}"
        notes_body = session.read_bytes("notes.md")
        assert b"This archive holds two members" in notes_body, \
            f"notes.md content wrong: {notes_body[:50]!r}"
        print(f"[2] py7zz walked .rar; members = {names}")

    # 3. End-to-end ingest.
    _install_fakes()
    await _create_schema()
    file_id = await _seed(rar_bytes, "fixture.rar")
    await _ingest(file_id)

    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.kind == "container", f"kind={f.kind!r}"
        assert f.ingest_status == "done", f"status={f.ingest_status}"
        peeks = f.description.get("member_peeks") or []
        kinds = [p["kind"] for p in peeks]
        assert "text" in kinds, f"expected text peek; got {kinds}"
        text_peek = next(p for p in peeks if p["kind"] == "text")
        assert "Test" in text_peek["preview"] or \
               "alpha" in text_peek["preview"], \
            f"text peek body missing: {text_peek['preview']!r}"
    print("[3] ingest produced container kind + text peek with body content")

    # 4. read_segment dispatch into the inner text member.
    storage = get_storage()
    async with factory() as s:
        f = await s.get(File, file_id)
        seg = await pipe.read_segment(
            file_row=f, args={"member_path": "data.txt"}, storage=storage,
        )
        assert seg.error is None, f"err: {seg.error!r}"
        assert "alpha" in (seg.text or ""), \
            f"data.txt content not returned: {seg.text!r}"
    print("[4] read_segment(member_path='data.txt') dispatched OK")

    print("\nALL RAR E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
