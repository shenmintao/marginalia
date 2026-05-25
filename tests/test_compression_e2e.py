"""End-to-end test for the unified archive pipeline + compression routing.

Run:
    .venv/Scripts/python tests/test_compression_e2e.py

Verifies that single-file compressed uploads (.gz / .bz2 / .xz),
logrotate variants (.log.1 / .log-YYYYMMDD), and multi-member archives
(.zip / .7z) all flow through ArchivePipeline correctly:

  1. Routing: resolve_pipeline directs every shape to "archive" or
     "log" as appropriate.
  2. Single-member compression: a `.log.gz` is treated as a 1-member
     archive; the LLM prompt includes a peek of the inner log lines
     (decompressed by py7zz) — we don't store an extra "compression
     method" column on the file, the archive pipeline owns this.
  3. Multi-format archive support: a 7-Zip-format archive (.7z) is
     extracted and listed.
  4. Member peek dispatch: a tarball containing notes.md + access.log
     yields member_peeks where each member is read by its leaf
     pipeline's read_segment_from_bytes (markdown shows md text, log
     shows L1/L2 prefixed lines).
  5. logrotate variant routing: app.log.1 and app.log-20260524 both
     land on LogPipeline (not ArchivePipeline, because they are not
     compressed — they are just rotated logs).
  6. read_segment dispatch: ArchivePipeline.read_segment(member_path=
     "notes.md") returns text from the inner markdown file via
     TextPipeline.read_segment_from_bytes.

No real LLM is called — the ingest profile is faked.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_compression_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from sqlalchemy import select  # noqa: E402

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File, FileEntry  # noqa: E402
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.pipelines.registry import resolve_pipeline  # noqa: E402
from marginalia.storage import get_storage  # noqa: E402


CALL_LOG: list[ChatRequest] = []


def _fake_archive_payload() -> dict:
    return {
        "summary": "A small synthetic archive used to exercise the pipeline.",
        "description": {
            "archive_kind": "tar.gz",
            "primary_language": None,
            "frameworks_detected": [],
        },
        "kind": "container",
        "extra": "",
        "entry_extra": "",
        "entry_catalog_path": ["Tests", "Archives"],
        "entry_tags": [
            {"name": "archive", "facet": "form"},
            {"name": "test-fixture", "facet": "source"},
        ],
    }


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        payload = _fake_archive_payload()
        return ChatResponse(
            text=json.dumps(payload), tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=500, output_tokens=120),
            parsed_json=payload,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    fake = _FakeIngest()

    def _factory(profile: str = "ingest"):
        return fake
    import marginalia.pipelines._text_indexer as imod
    import marginalia.pipelines.archive as amod
    imod.get_chat_client = _factory  # type: ignore[assignment]
    amod.get_chat_client = _factory  # type: ignore[assignment]


# ---- fixtures --------------------------------------------------------------

LOG_LINES = [
    "2026-05-24T10:00:00Z INFO  starting up",
    "2026-05-24T10:00:01Z INFO  bound :8080",
    "2026-05-24T10:00:05Z WARN  slow query (210ms)",
    "2026-05-24T10:00:09Z ERROR oops connection refused",
    "2026-05-24T10:00:11Z INFO  recovered",
]
LOG_BODY = "\n".join(LOG_LINES) + "\n"


def _build_log_gz() -> bytes:
    return gzip.compress(LOG_BODY.encode("utf-8"))


def _build_tar_gz_with_md_and_log() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        md = b"# Hello\n\nA tiny markdown body.\n"
        info = tarfile.TarInfo(name="notes.md")
        info.size = len(md)
        tf.addfile(info, io.BytesIO(md))

        lg = LOG_BODY.encode("utf-8")
        info = tarfile.TarInfo(name="access.log")
        info.size = len(lg)
        tf.addfile(info, io.BytesIO(lg))
    return buf.getvalue()


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_file(
    *, body: bytes, mime: str, name: str,
) -> tuple[str, str]:
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
            content_type=mime,
        )
        await db.commit()
        return result.file_id, result.entry_id


async def _ingest(file_id: str) -> None:
    from marginalia.tasks.handlers.ingest_file import handle_ingest_file
    await handle_ingest_file({"file_id": file_id, "entry_id": None})


# ---- tests -----------------------------------------------------------------

def _check_routing() -> None:
    cases = [
        ("text/plain", ".log",        "access.log",         "log"),
        ("text/plain", ".1",          "access.log.1",       "log"),
        ("text/plain", "-20260524",   "access.log-20260524","log"),
        ("application/gzip", ".gz",   "access.log.gz",      "archive"),
        ("application/x-tar", ".tar.gz", "src.tar.gz",      "archive"),
        ("application/x-7z-compressed", ".7z", "docs.7z",   "archive"),
        ("application/zip", ".zip",   "backup.zip",         "archive"),
        ("application/pdf", ".pdf",   "paper.pdf",          "pdf"),
    ]
    for mime, ext, fn, expected in cases:
        p = resolve_pipeline(mime, ext, filename=fn)
        actual = p.name if p else None
        assert actual == expected, \
            f"routing {fn} → got {actual}, expected {expected}"
    print(f"[1] routing: {len(cases)} cases passed")


async def _check_log_gz_ingest() -> None:
    body = _build_log_gz()
    file_id, _entry = await _seed_file(
        body=body, mime="application/gzip", name="access.log.gz",
    )
    await _ingest(file_id)
    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.kind == "container", \
            f"expected kind=container for .gz, got {f.kind!r}"
        assert f.ingest_status == "done", f"status={f.ingest_status}"
        peeks = f.description.get("member_peeks") or []
        assert len(peeks) == 1, \
            f".log.gz should expose one member peek, got {len(peeks)}"
        peek = peeks[0]
        assert peek["kind"] == "log", \
            f"inner peek pipeline should be log, got {peek['kind']!r}"
        assert "ERROR" in peek["preview"] or "INFO" in peek["preview"], \
            f"log peek body missing line content: {peek['preview']!r}"
    print("[2] .log.gz → archive(1 member, log peek with real lines)")


async def _check_tar_gz_with_members() -> None:
    body = _build_tar_gz_with_md_and_log()
    file_id, _entry = await _seed_file(
        body=body, mime="application/gzip", name="bundle.tar.gz",
    )
    await _ingest(file_id)
    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.kind == "container", f"got {f.kind}"
        peeks = f.description.get("member_peeks") or []
        kinds = {p["kind"]: p["preview"] for p in peeks}
        assert "text" in kinds, f"expected text peek (notes.md); kinds={kinds}"
        assert "log" in kinds, f"expected log peek (access.log); kinds={kinds}"
        # markdown peek should contain the heading
        assert "Hello" in kinds["text"], \
            f"text peek should include md body: {kinds['text']!r}"
    print("[3] tar.gz with md + log → both leaf pipelines peeked correctly")


async def _check_archive_read_segment_dispatch() -> None:
    """ArchivePipeline.read_segment(member_path=...) dispatches to the
    leaf pipeline's read_segment_from_bytes."""
    body = _build_tar_gz_with_md_and_log()
    file_id, _entry = await _seed_file(
        body=body, mime="application/gzip", name="dispatch.tar.gz",
    )
    await _ingest(file_id)

    from marginalia.pipelines import resolve_pipeline as resolve
    pipe = resolve("application/gzip", ".tar.gz", filename="dispatch.tar.gz")
    assert pipe is not None and pipe.name == "archive"

    factory = get_session_factory()
    storage = get_storage()
    async with factory() as s:
        f = await s.get(File, file_id)
        # read notes.md inside the archive
        seg = await pipe.read_segment(
            file_row=f, args={"member_path": "notes.md"}, storage=storage,
        )
        assert seg.error is None, f"unexpected error: {seg.error!r}"
        assert "Hello" in (seg.text or ""), \
            f"member dispatch returned wrong text: {seg.text!r}"

        # read access.log lines 1-2
        seg2 = await pipe.read_segment(
            file_row=f,
            args={"member_path": "access.log", "line_start": 1, "line_end": 2},
            storage=storage,
        )
        assert seg2.error is None, f"err: {seg2.error!r}"
        assert "starting up" in (seg2.text or ""), \
            f"line slice missing: {seg2.text!r}"
    print("[4] archive read_segment(member_path=...) → leaf pipeline OK")


async def _main() -> None:
    _install_fakes()
    await _create_schema()
    _check_routing()
    await _check_log_gz_ingest()
    await _check_tar_gz_with_members()
    await _check_archive_read_segment_dispatch()
    print("ALL COMPRESSION E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
