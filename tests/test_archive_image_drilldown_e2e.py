"""Archive-internal image drill-down — agent-time VLM call.

Run:
    .venv/Scripts/python tests/test_archive_image_drilldown_e2e.py

Verifies the asymmetry we deliberately built into ArchivePipeline:

  - Ingest:  archive members of kind=image get a structural placeholder
             only; we do NOT call the VLM. A 100-image zip would burn
             VLM cost otherwise.
  - Drill:   when the agent calls read_segment(member_path="photo.png"),
             ArchivePipeline routes the bytes to image.read_segment_from_bytes
             which DOES call the VLM (downscaled to 1568px / JPEG 85)
             and returns a 1-2 sentence caption.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import zipfile
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_archive_image_drilldown_e2e_data"
if _TEST_ROOT.exists():
    shutil.rmtree(_TEST_ROOT)
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from marginalia.config import get_settings  # noqa: E402

get_settings.cache_clear()  # type: ignore[attr-defined]

from marginalia import llm  # noqa: E402
from marginalia.db.engine import get_engine, get_session_factory  # noqa: E402
from marginalia.db.models import Base, File  # noqa: E402
from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage  # noqa: E402
from marginalia.pipelines import resolve_pipeline  # noqa: E402
from marginalia.storage import get_storage  # noqa: E402


VISION_CALLS: list[ChatRequest] = []
INGEST_CALLS: list[ChatRequest] = []


def _build_png() -> bytes:
    """A tiny solid-colour PNG so we don't depend on a sample fixture."""
    from PIL import Image
    img = Image.new("RGB", (32, 32), (160, 200, 220))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _build_zip_with_image() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("readme.md", "# Test archive\n\nContains one image.\n")
        zf.writestr("photo.png", _build_png())
    return buf.getvalue()


# ---- fakes -----------------------------------------------------------------

class _FakeVision:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        VISION_CALLS.append(request)
        return ChatResponse(
            text="A small solid pale-blue square; appears to be a placeholder graphic.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=300, output_tokens=20),
            parsed_json=None,
        )


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        INGEST_CALLS.append(request)
        payload = {
            "summary": "A tiny archive with a markdown readme and one image.",
            "description": {
                "archive_kind": "zip",
                "primary_language": None,
                "frameworks_detected": [],
            },
            "kind": "container",
            "extra": "",
            "entry_extra": "",
            "entry_catalog_path": ["Tests"],
            "entry_tags": [{"name": "test", "facet": "source"}],
        }
        return ChatResponse(
            text=json.dumps(payload), tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=80),
            parsed_json=payload,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    fake_v = _FakeVision()
    fake_i = _FakeIngest()

    def _factory(profile: str = "ingest"):
        if profile == "vision":
            return fake_v
        return fake_i

    import marginalia.pipelines.archive as amod
    import marginalia.pipelines.image as immod
    amod.get_chat_client = _factory  # type: ignore[assignment]
    immod.get_chat_client = _factory  # type: ignore[assignment]


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
            content_type="application/zip",
        )
        await db.commit()
        return result.file_id


async def _ingest(file_id: str) -> None:
    from marginalia.tasks.handlers.ingest_file import handle_ingest_file
    await handle_ingest_file({"file_id": file_id, "entry_id": None})


# ---- test ------------------------------------------------------------------

async def _main() -> None:
    _install_fakes()
    await _create_schema()

    body = _build_zip_with_image()
    print(f"[setup] zip with image: {len(body)} bytes")

    file_id = await _seed(body, "with_image.zip")
    await _ingest(file_id)

    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.ingest_status == "done", f"status={f.ingest_status}"

    # Asymmetry assertion #1: ingest must NOT have called vision.
    assert len(VISION_CALLS) == 0, \
        f"ingest should not call VLM for archive-internal images; "\
        f"got {len(VISION_CALLS)} calls"
    print(f"[1] ingest done — vision_calls={len(VISION_CALLS)} "
          f"(image member kept structural-placeholder during ingest)")

    # Asymmetry assertion #2: peek for the image is a placeholder.
    async with factory() as s:
        f = await s.get(File, file_id)
        peeks = f.description.get("member_peeks") or []
        image_peek = next((p for p in peeks if p["kind"] == "image"), None)
        assert image_peek is not None, f"no image peek; peeks={peeks}"
        assert image_peek["preview"].startswith("[image:"), \
            f"image peek should be placeholder, got: {image_peek['preview']!r}"
        print(f"[2] image peek: {image_peek['preview']!r}")

    # Now drill in: agent calls read_segment with member_path.
    pipe = resolve_pipeline("application/zip", ".zip", filename="with_image.zip")
    assert pipe is not None and pipe.name == "archive"

    storage = get_storage()
    async with factory() as s:
        f = await s.get(File, file_id)
        seg = await pipe.read_segment(
            file_row=f, args={"member_path": "photo.png"}, storage=storage,
        )
    assert seg.error is None, f"unexpected error: {seg.error!r}"
    assert seg.text and len(seg.text) > 10, f"empty caption: {seg.text!r}"
    print(f"[3] drill-down caption: {seg.text!r}")

    # Asymmetry assertion #3: vision was now called (exactly once).
    assert len(VISION_CALLS) == 1, \
        f"drill-down should trigger one VLM call; got {len(VISION_CALLS)}"
    print(f"[4] vision_calls after drill = {len(VISION_CALLS)} (lazy as designed)")

    print("\nALL ARCHIVE_IMAGE_DRILLDOWN E2E CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(_main())
