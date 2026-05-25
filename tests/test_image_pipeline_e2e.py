"""End-to-end image_pipeline + vision profile sanity check.

Run:
    .venv/Scripts/python tests/test_image_pipeline_e2e.py

Verifies:
  1. The pipeline registry routes image/* mimes to ImagePipeline.
  2. Uploading a PNG enqueues ingest_file; the handler picks it up.
  3. The fake VISION client receives a ChatRequest whose user message
     contains:
       - one TextBlock (the prompt context)
       - one ImageBlock with media_type='image/png' and base64-decoded
         bytes equal to the original upload
  4. The pipeline produces a PipelineResult with kind='image' and a
     description.regions array; the handler writes it to files.* and
     creates the catalog/tag chain as for text.
  5. files.summary / kind / description / entry_tags are all set.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_image_pipeline_e2e_data"
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
from marginalia.db.models import Base, EntryTag, File, FileEntry, Tag
from marginalia.llm.types import (
    ChatRequest, ChatResponse, ImageBlock, TextBlock, TokenUsage,
)
from marginalia.main import app
from marginalia.tasks.kinds import KIND_INGEST_FILE
from marginalia.tasks.runner import TaskRunner


CALL_LOG: list[ChatRequest] = []


def _make_1x1_png() -> bytes:
    """Build the minimum valid PNG: 1×1 black pixel, no library required."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(typ: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + typ + data
            + zlib.crc32(typ + data).to_bytes(4, "big")
        )

    ihdr = _chunk(b"IHDR", b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00")
    raw = b"\x00\x00\x00\x00"  # filter byte + RGB triplet
    idat = _chunk(b"IDAT", zlib.compress(raw))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


PNG_BYTES = _make_1x1_png()


# ---- fake vision client -----------------------------------------------------

class _FakeVision:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        CALL_LOG.append(request)
        payload = {
            "summary": "A 1x1 test image used to exercise the vision path.",
            "description": {
                "regions": [
                    {
                        "id": "r1",
                        "label": "Solid color region",
                        "summary": "The entire frame is uniform.",
                        "key_terms": ["test", "uniform", "solid"],
                    }
                ]
            },
            "kind": "image",
            "extra": "Synthetic test fixture — no real semantic content.",
            "entry_extra": "Sits among test fixtures in /tests/images.",
            "entry_catalog_path": ["Tests", "Images"],
            "entry_tags": [
                {"name": "test-fixture", "facet": "source"},
                {"name": "png", "facet": "form"},
                {"name": "synthetic", "facet": "topic"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=1500, output_tokens=200, cache_read_tokens=1200),
            parsed_json=payload,
        )


def _install_fake() -> None:
    llm.reset_clients_cache()
    fake = _FakeVision()
    def _factory(profile: str = "ingest"):
        return fake
    import marginalia.pipelines.image as imod
    imod.get_chat_client = _factory  # type: ignore[assignment]


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_task_done(task_id: str, timeout: float = 10.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (
                await s.execute(text(
                    "SELECT status FROM tasks WHERE id = :id"
                ), {"id": task_id})
            ).first()
            if row is None:
                raise RuntimeError("task disappeared")
            (status,) = row
            if status in ("done", "dead"):
                return status
        await asyncio.sleep(0.1)
    raise TimeoutError(f"task {task_id} did not finish")


async def main():
    _install_fake()
    await _create_schema()

    runner = TaskRunner()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/tests/images/"},
                    files={"file": ("pixel.png", io.BytesIO(PNG_BYTES), "image/png")},
                )
                assert r.status_code == 201, r.text
                up = r.json()
                file_id = up["file_id"]
                entry_id = up["entry_id"]
                print("[upload]", up)

            factory = get_session_factory()
            async with factory() as s:
                task_id = (
                    await s.execute(text(
                        "SELECT id FROM tasks WHERE kind = :k AND payload LIKE :p"
                    ), {"k": KIND_INGEST_FILE, "p": f'%"{file_id}"%'})
                ).scalar_one()

            status = await _wait_for_task_done(task_id, timeout=10.0)
            assert status == "done", f"ingest failed: {status}"
            print("[task] done")
        finally:
            await runner.stop()

    # ---- 1. The vision client was actually invoked ------------------------
    assert len(CALL_LOG) == 1, f"expected 1 vision call, got {len(CALL_LOG)}"
    req = CALL_LOG[0]

    # ---- 2. Inspect the user message blocks ------------------------------
    user_msg = req.messages[0]
    assert user_msg.role == "user"
    blocks = user_msg.content
    assert isinstance(blocks, list), "user content should be block list, not str"

    text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
    image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
    print("[blocks] text:", len(text_blocks), " image:", len(image_blocks))
    assert len(text_blocks) == 1
    assert len(image_blocks) == 1

    img = image_blocks[0]
    # The pipeline now down-scales + re-encodes everything as JPEG before
    # sending to the VLM (bounded long edge, predictable size). The
    # post-rescale bytes are no longer byte-equal to the upload, but the
    # media_type should always be image/jpeg and the data should decode.
    assert img.media_type == "image/jpeg", \
        f"bad media_type: {img.media_type}"

    decoded = base64.b64decode(img.data_b64)
    assert decoded.startswith(b"\xff\xd8\xff"), \
        "expected JPEG magic header after rescale"
    print("[image] post-rescale JPEG OK; len =", len(decoded))

    # ---- 3. DB-level: file content fields written ------------------------
    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        e = await s.get(FileEntry, entry_id)
        assert f.kind == "image"
        assert f.summary and "test image" in f.summary.lower()
        assert isinstance(f.description, dict)
        assert "regions" in f.description
        assert len(f.description["regions"]) == 1
        assert f.ingest_status == "done"
        assert f.ingested_at is not None

        # entry catalog path → Tests/Images
        from marginalia.db.models import Catalog
        cat = await s.get(Catalog, e.catalog_id)
        assert cat.name == "Images"
        parent = await s.get(Catalog, cat.parent_id)
        assert parent.name == "Tests"

        # entry tags
        tag_pairs = (
            await s.execute(
                select(Tag.name, Tag.facet)
                .join(EntryTag, Tag.id == EntryTag.tag_id)
                .where(EntryTag.entry_id == entry_id)
            )
        ).all()
        names_facets = {(n, f) for n, f in tag_pairs}
        print("[tags]", names_facets)
        assert ("test-fixture", "source") in names_facets
        assert ("png", "form") in names_facets
        assert ("synthetic", "topic") in names_facets

    # ---- 4. The system prompt embedded was the vision-specific one -------
    assert "image indexer" in (req.system or "").lower(), \
        "did not see vision system prompt"

    print("\nALL IMAGE_PIPELINE E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
