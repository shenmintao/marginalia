"""End-to-end container pipeline + analyze_container tool (Cycle 21).

Run:
    .venv/Scripts/python tests/test_container_e2e.py

Verifies:
  Pipeline (ingest):
    1. A zip with `.git/HEAD` + README.md + pyproject.toml + src/main.py
       + node_modules/ignored.js triggers container_pipeline.
    2. The fake `ingest` LLM receives a prompt with the directory tree
       and key file contents (README, pyproject), but the prompt does
       NOT include `node_modules/ignored.js` (filtered).
    3. After ingest: files.kind='container', description has
       container_kind='git_repo', tree, indexed_files, key_files.
    4. Path traversal members are rejected (zip with `../escape.txt`).

  Tool (agent-time):
    5. analyze_container with list_files glob='**/*.py' returns only
       python files inside src/.
    6. analyze_container with read_files reads specific lines from a
       file inside the container.
    7. analyze_container with search finds matches across multiple
       container files with line numbers and context.
    8. Wrong entry kind (non-container) rejected.
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

_TEST_ROOT = Path(__file__).resolve().parent / "_container_e2e_data"
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

from marginalia import llm
from marginalia.agent.tools import ToolContext, get_tool
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, EntryTag, File, FileEntry, Tag
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app
from marginalia.tasks.runner import TaskRunner


CALL_LOG: list[ChatRequest] = []


def _build_repo_zip() -> bytes:
    """Build a zip simulating a small git repo."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".git/HEAD", "ref: refs/heads/main\n")
        zf.writestr(".git/config", "[core]\n\trepositoryformatversion = 0\n")
        zf.writestr("README.md",
                    "# myapp\n\nA toy FastAPI service.\n\n"
                    "## Setup\nInstall deps and run uvicorn.\n")
        zf.writestr("pyproject.toml",
                    "[project]\nname = \"myapp\"\n"
                    "dependencies = [\"fastapi\", \"sqlalchemy\"]\n")
        zf.writestr("src/__init__.py", "")
        zf.writestr("src/main.py",
                    "from fastapi import FastAPI\n\n"
                    "app = FastAPI()\n\n"
                    "@app.get('/health')\n"
                    "def health():\n"
                    "    return {'status': 'ok'}\n\n"
                    "@app.get('/users/{uid}')\n"
                    "def get_user(uid: int):\n"
                    "    return {'id': uid}\n")
        zf.writestr("src/db.py",
                    "import sqlalchemy\n\n"
                    "engine = sqlalchemy.create_engine('sqlite:///app.db')\n")
        zf.writestr("tests/test_main.py",
                    "def test_health():\n"
                    "    assert 1 == 1\n")
        # ignored
        zf.writestr("node_modules/leftpad/index.js",
                    "module.exports = function(s,n){return s;}\n")
        zf.writestr("yarn.lock", "# yarn lockfile\n")
    return buf.getvalue()


def _build_traversal_zip() -> bytes:
    """A zip with a path-traversal member; should be filtered."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", "# safe\n")
        zf.writestr("../escape.txt", "should be rejected")
    return buf.getvalue()


# ---- fake ingest -----------------------------------------------------------

class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        payload = {
            "summary": "Toy FastAPI repo with src/ and tests/.",
            "description": {
                "primary_language": "python",
                "frameworks_detected": ["FastAPI", "SQLAlchemy"],
            },
            "kind": "container",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Code", "WebApps"],
            "entry_tags": [
                {"name": "fastapi", "facet": "topic"},
                {"name": "python", "facet": "language"},
                {"name": "git_repo", "facet": "form"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2200, output_tokens=400, cache_read_tokens=1800),
            parsed_json=payload,
        )


def _install_fake() -> None:
    llm.reset_clients_cache()
    fake = _FakeIngest()
    def _factory(profile: str = "ingest"):
        return fake
    import marginalia.pipelines.archive as cmod
    cmod.get_chat_client = _factory  # type: ignore[assignment]


async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_status(file_id: str, *, expect: str,
                           timeout: float = 12.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (await s.execute(text(
                "SELECT ingest_status FROM files WHERE id=:id"
            ), {"id": file_id})).first()
            if row is None:
                raise RuntimeError("file vanished")
            (status,) = row
            if status == expect or status in ("done", "failed", "dead"):
                return status
        await asyncio.sleep(0.05)
    raise TimeoutError("ingest did not finish")


async def main():
    _install_fake()
    await _create_schema()

    repo_zip = _build_repo_zip()
    traversal_zip = _build_traversal_zip()

    runner = TaskRunner()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                # ---- 1. ingest the repo zip ---------------------------
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/repos/"},
                    files={"file": ("myapp.zip", io.BytesIO(repo_zip),
                                    "application/zip")},
                )
                assert r.status_code == 201, r.text
                up = r.json()
                file_id = up["file_id"]
                entry_id = up["entry_id"]
                status = await _wait_for_status(file_id, expect="done")
                assert status == "done", f"ingest failed: {status}"
                print("[1] container ingested; file =", file_id[:8])

                # ---- 2. fake LLM call inspection -----------------------
                assert len(CALL_LOG) == 1
                prompt = CALL_LOG[0].messages[0].content[0].text  # type: ignore[index, attr-defined]
                assert "README.md" in prompt
                assert "pyproject.toml" in prompt
                assert "FastAPI" in prompt
                assert "node_modules" not in prompt, \
                    "node_modules leaked into LLM prompt"
                assert "yarn.lock" not in prompt
                print("[2] prompt has key files; ignore-list filtered")

                # ---- 3. DB invariants ---------------------------------
                factory = get_session_factory()
                async with factory() as s:
                    f = await s.get(File, file_id)
                    assert f.kind == "container"
                    desc = f.description
                    assert isinstance(desc, dict)
                    assert desc["container_kind"] == "git_repo"
                    assert desc["primary_language"] == "python"
                    assert "FastAPI" in (desc["frameworks_detected"] or [])
                    indexed = desc["indexed_files"]
                    paths = {e["path"] for e in indexed}
                    assert "README.md" in paths
                    assert "src/main.py" in paths
                    assert "tests/test_main.py" in paths
                    # ignored
                    assert "node_modules/leftpad/index.js" not in paths
                    assert "yarn.lock" not in paths
                    print("[3] description.indexed_files filtered correctly:",
                          len(paths), "files")

                # ---- 4. traversal zip rejected --------------------------
                CALL_LOG.clear()
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/repos/", "display_name": "bad.zip"},
                    files={"file": ("bad.zip", io.BytesIO(traversal_zip),
                                    "application/zip")},
                )
                assert r.status_code == 201, r.text
                bad_file_id = r.json()["file_id"]
                status = await _wait_for_status(bad_file_id, expect="done")
                assert status == "done"
                async with factory() as s:
                    bad = await s.get(File, bad_file_id)
                    bad_paths = {e["path"] for e in bad.description["indexed_files"]}
                # ../escape.txt must NOT be present
                assert "../escape.txt" not in bad_paths
                assert all("escape" not in p for p in bad_paths), \
                    f"path-traversal member leaked: {bad_paths}"
                print("[4] path-traversal member filtered")

        finally:
            await runner.stop()

    # ---- 5. analyze_container list_files glob ----------------------------
    factory = get_session_factory()

    async def _call(args: dict) -> dict:
        reg = get_tool("analyze_container")
        async with factory() as s:
            ctx = ToolContext(session_id="s", conversation_id="c")
            return await reg.handler(s, ctx, args)

    res = await _call({
        "container_entry_id": entry_id,
        "list_files": {"glob": "**/*.py"},
    })
    py_paths = [m["path"] for m in res["files"]["matches"]]
    print("[5] *.py files in container:", py_paths)
    assert all(p.endswith(".py") for p in py_paths)
    assert "src/main.py" in py_paths
    assert "tests/test_main.py" in py_paths

    # ---- 6. read_files lines ---------------------------------------------
    res = await _call({
        "container_entry_id": entry_id,
        "read_files": [
            {"path": "src/main.py",
             "locations": [{"unit": "lines", "value": "1-5"}]},
        ],
    })
    print("[6] read_files src/main.py 1-5:",
          res["reads"][0]["locations"][0].get("text", "")[:80])
    text_part = res["reads"][0]["locations"][0]["text"]
    assert "FastAPI" in text_part

    # ---- 7. search across files ------------------------------------------
    res = await _call({
        "container_entry_id": entry_id,
        "search": {"pattern": "FastAPI", "context_lines": 1},
    })
    hits = res["search"]["hits"]
    print("[7] search 'FastAPI' hits:", len(hits))
    assert any(h["path"] == "src/main.py" for h in hits)
    assert any(h["path"] == "README.md" for h in hits)

    # ---- 8. wrong-kind entry rejected ------------------------------------
    # Upload a non-container file and confirm analyze_container rejects.
    transport2 = ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport2, base_url="http://t") as c:
            r = await c.post(
                "/v1/upload",
                params={"remote_path": "/notes/"},
                files={"file": ("note.md", io.BytesIO(b"# Hello\n\nA note.\n"),
                                "text/markdown")},
            )
            assert r.status_code == 201
            note_entry_id = r.json()["entry_id"]
    res = await _call({"container_entry_id": note_entry_id})
    assert "error" in res and "container" in res["error"].lower()
    print("[8] non-container rejected:", res["error"])

    print("\nALL CONTAINER E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
