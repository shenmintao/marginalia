"""Git-repo container metadata extraction (Cycle 26).

Run:
    .venv/Scripts/python tests/test_git_repo_e2e.py

Verifies:
  1. parse() reads .git/HEAD and resolves the current branch.
  2. parse() reads .git/refs/heads/<branch> for the head hash.
  3. parse() walks .git/logs/HEAD into recent_commits + dedup'd authors.
  4. parse() extracts remote URLs from .git/config.
  5. Container pipeline now writes git_metadata into description for
     git_repo containers.
  6. Non-git container: git_metadata is None.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_git_repo_e2e_data"
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
from marginalia.db.engine import get_engine, get_session_factory
from marginalia.db.models import Base, File, FileEntry
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage
from marginalia.main import app
from marginalia.pipelines.git_metadata import parse as parse_git
from marginalia.tasks.runner import TaskRunner


# Realistic-shaped reflog: hash old, hash new, "Name <email>" UNIX_TS TZ \t msg
_REFLOG_BODY = """
0000000000000000000000000000000000000000 abc1111111111111111111111111111111111111 Alice Chen <alice@example.com> 1700000000 +0800\tcommit (initial): repo created
abc1111111111111111111111111111111111111 def2222222222222222222222222222222222222 Bob Liu <bob@example.com> 1700000600 +0800\tcommit: add main module
def2222222222222222222222222222222222222 ace3333333333333333333333333333333333333 Alice Chen <alice@example.com> 1700001200 +0800\tcommit: add tests
ace3333333333333333333333333333333333333 fed4444444444444444444444444444444444444 Carol Wong <carol@example.com> 1700001800 +0800\tcommit: ci config
""".lstrip("\n")


def _build_repo_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(".git/HEAD", "ref: refs/heads/main\n")
        zf.writestr(".git/refs/heads/main",
                    "fed4444444444444444444444444444444444444\n")
        zf.writestr(".git/logs/HEAD", _REFLOG_BODY)
        zf.writestr(".git/config",
                    "[core]\n\trepositoryformatversion = 0\n"
                    "[remote \"origin\"]\n\turl = git@github.com:acme/repo.git\n"
                    "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
                    "[branch \"main\"]\n\tremote = origin\n")
        zf.writestr("README.md", "# acme repo\n\nA test fixture.\n")
        zf.writestr("src/main.py", "print('hi')\n")
    return buf.getvalue()


# ---- 1. Pure parse() unit -------------------------------------------------

def _unit_test_parse():
    print("[unit] parsing synthetic .git layout...")
    repo_zip = _build_repo_zip()
    workdir = Path(tempfile.mkdtemp(prefix="marg-git-test-"))
    try:
        with zipfile.ZipFile(io.BytesIO(repo_zip)) as zf:
            zf.extractall(workdir)
        meta = parse_git(workdir)
        assert meta is not None
        assert meta.branch == "main"
        assert meta.head_hash == "fed4444444444444444444444444444444444444"
        # 4 distinct hashes in reflog
        assert len(meta.recent_commits) == 4
        first = meta.recent_commits[0]  # most recent first
        assert first.hash == "fed4444444444444444444444444444444444444"
        assert first.author_name == "Carol Wong"
        assert first.message_first_line == "commit: ci config"
        # 3 distinct authors
        author_ids = {c.author_email for c in meta.recent_commits}
        assert author_ids == {"alice@example.com", "bob@example.com",
                              "carol@example.com"}
        assert meta.remotes.get("origin") == "git@github.com:acme/repo.git"
        print(f"[unit] branch={meta.branch} head={meta.head_hash[:8]} "
              f"commits={len(meta.recent_commits)} "
              f"authors={len(meta.authors)} remotes={list(meta.remotes)}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ---- 2. End-to-end through the container pipeline -------------------------

class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        # Verify the prompt actually carries git_metadata
        ut = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
        global LAST_PROMPT
        LAST_PROMPT = ut
        payload = {
            "summary": "An acme repository.",
            "description": {
                "primary_language": "python",
                "frameworks_detected": [],
            },
            "kind": "container",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Code"],
            "entry_tags": [
                {"name": "git", "facet": "form"},
                {"name": "python", "facet": "language"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1500, output_tokens=200,
                             cache_read_tokens=1200),
            parsed_json=payload,
        )


LAST_PROMPT: str = ""


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


async def _wait_for_done(file_id: str, *, timeout: float = 12.0) -> str:
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
            if status in ("done", "failed", "dead"):
                return status
        await asyncio.sleep(0.05)
    raise TimeoutError("ingest did not finish")


async def main():
    global LAST_PROMPT
    _unit_test_parse()
    print("[1] parse() unit tests passed")

    _install_fake()
    await _create_schema()
    repo_zip = _build_repo_zip()

    runner = TaskRunner()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport,
                                         base_url="http://t") as c:
                # 2. Upload + ingest a git repo zip
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/repos/"},
                    files={"file": ("acme.zip", io.BytesIO(repo_zip),
                                    "application/zip")},
                )
                assert r.status_code == 201, r.text
                file_id = r.json()["file_id"]
                status = await _wait_for_done(file_id)
                assert status == "done", f"ingest failed: {status}"

            # 3. The LLM prompt carried git_metadata
            print(f"[debug] LAST_PROMPT length: {len(LAST_PROMPT)}")
            print(f"[debug] git_metadata in prompt: {'git_metadata' in LAST_PROMPT}")
            print(f"[debug] 'main' in prompt: {'main' in LAST_PROMPT}")
            print(f"[debug] 'Alice' in prompt: {'Alice' in LAST_PROMPT}")
            assert "git_metadata" in LAST_PROMPT
            assert "main" in LAST_PROMPT  # branch name
            assert "Alice Chen" in LAST_PROMPT
            print("[2] LLM prompt contains git_metadata + branch + authors")

            # 4. files.description.git_metadata is populated
            factory = get_session_factory()
            async with factory() as s:
                f = await s.get(File, file_id)
                assert isinstance(f.description, dict)
                gm = f.description.get("git_metadata")
                assert gm is not None
                assert gm["branch"] == "main"
                assert gm["head_hash"] == "fed4444444444444444444444444444444444444"
                assert len(gm["recent_commits"]) == 4
                assert gm["remotes"]["origin"] == "git@github.com:acme/repo.git"
                print(f"[3] description.git_metadata: branch={gm['branch']} "
                      f"commits={len(gm['recent_commits'])} "
                      f"remotes={list(gm['remotes'])}")
        finally:
            await runner.stop()

    # 5. Non-git container has git_metadata = None
    LAST_PROMPT = ""
    runner2 = TaskRunner()
    async with app.router.lifespan_context(app):
        await runner2.start()
        try:
            non_git_zip = io.BytesIO()
            with zipfile.ZipFile(non_git_zip, "w") as zf:
                zf.writestr("README.md", "# nothing fancy\n")
                zf.writestr("data.txt", "just data\n")
            non_git_zip.seek(0)
            transport2 = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport2,
                                         base_url="http://t") as c:
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/archives/"},
                    files={"file": ("archive.zip", non_git_zip,
                                    "application/zip")},
                )
                assert r.status_code == 201, r.text
                ng_file_id = r.json()["file_id"]
                status = await _wait_for_done(ng_file_id)
                assert status == "done"

            factory = get_session_factory()
            async with factory() as s:
                f = await s.get(File, ng_file_id)
                gm = f.description.get("git_metadata")
                assert gm is None, f"non-git container has git_metadata: {gm}"
                assert f.description["container_kind"] == "zip_archive"
            print("[4] non-git container: git_metadata is None, container_kind=zip_archive")
        finally:
            await runner2.stop()

    print("\nALL GIT_REPO E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
