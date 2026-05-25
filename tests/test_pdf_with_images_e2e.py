"""End-to-end PDF pipeline with embedded figure description (Cycle 17b).

Run:
    .venv/Scripts/python tests/test_pdf_with_images_e2e.py

Verifies:
  1. A PDF with two embedded "figure" PNGs is processed by PdfPipeline.
  2. The vision profile receives one ChatRequest per significant figure,
     each carrying an ImageBlock with the right media_type.
  3. The ingest profile receives a prompt where each [Figure X.Y] line
     is appended to its corresponding page's text block.
  4. Tiny / icon-sized images are filtered out (not sent to VLM).
  5. The handler still produces a valid description.sections payload via
     the canned ingest LLM.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

_TEST_ROOT = Path(__file__).resolve().parent / "_pdf_with_images_e2e_data"
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
from marginalia.db.models import Base, EntryTag, File, FileEntry
from marginalia.llm.types import (
    ChatRequest, ChatResponse, ImageBlock, TextBlock, TokenUsage,
)
from marginalia.main import app
from marginalia.tasks.runner import TaskRunner


VISION_CALL_LOG: list[ChatRequest] = []
INGEST_CALL_LOG: list[ChatRequest] = []


def _make_solid_png(w: int = 200, h: int = 150,
                    rgb: tuple[int, int, int] = (40, 90, 200),
                    noisy: bool = True) -> bytes:
    """Build a PNG of given size using only stdlib zlib.

    By default `noisy=True` introduces per-pixel jitter so the PNG
    compresses to tens of KB (typical of real photos / charts) rather
    than the few-hundred bytes a flat-color PNG produces. This matters
    for production filters that reject sub-5KB images as icons; the test
    needs fixtures that survive those filters."""
    sig = b"\x89PNG\r\n\x1a\n"

    def _chunk(typ: bytes, data: bytes) -> bytes:
        return (
            len(data).to_bytes(4, "big")
            + typ + data
            + zlib.crc32(typ + data).to_bytes(4, "big")
        )

    ihdr = _chunk(b"IHDR",
                  w.to_bytes(4, "big") + h.to_bytes(4, "big") +
                  b"\x08\x02\x00\x00\x00")
    raw = bytearray()
    if noisy:
        # deterministic pseudo-random per pixel
        seed = (rgb[0] * 31 + rgb[1] * 53 + rgb[2] * 97) & 0xFFFFFFFF
        for y in range(h):
            raw.append(0)  # filter byte
            for x in range(w):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                jitter = (seed % 64) - 32
                raw.append(max(0, min(255, rgb[0] + jitter)))
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                jitter = (seed % 64) - 32
                raw.append(max(0, min(255, rgb[1] + jitter)))
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                jitter = (seed % 64) - 32
                raw.append(max(0, min(255, rgb[2] + jitter)))
    else:
        row = b"\x00" + bytes(rgb) * w
        for _ in range(h):
            raw += row
    idat = _chunk(b"IDAT", zlib.compress(bytes(raw)))
    iend = _chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


def _build_pdf_with_images() -> bytes:
    from fpdf import FPDF

    big = _make_solid_png(220, 160, (200, 80, 50))     # significant (noisy)
    big2 = _make_solid_png(180, 120, (50, 180, 100))   # significant (noisy)
    icon = _make_solid_png(20, 20, (255, 255, 255), noisy=False)  # too small

    paths = []
    for png in (big, big2, icon):
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        f.write(png)
        f.close()
        paths.append(f.name)

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text=(
        "Page 1: Introduction. This section discusses Raft consensus, "
        "leader election, log replication, and the safety properties "
        "that make Raft easier to understand than Paxos. The figure "
        "below illustrates the leader election timing diagram."
    ))
    pdf.image(paths[0], x=10, y=70, w=80, h=60)        # fig 1.1 (big)
    pdf.image(paths[2], x=100, y=70, w=8, h=8)         # icon (filtered)

    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text=(
        "Page 2: Pipeline. We now describe Paxos, a classical "
        "majority-quorum consensus algorithm. Acceptors, proposers, "
        "and learners coordinate through phase 1 prepare and phase 2 "
        "accept messages. The figure shows the message flow."
    ))
    pdf.image(paths[1], x=10, y=70, w=70, h=50)        # fig 2.1 (big)

    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text=(
        "Page 3: Conclusion. We compared Raft and Paxos across "
        "ergonomics, performance, and pedagogical clarity. Future work "
        "includes Byzantine extensions and geo-replication considerations."
    ))
    return bytes(pdf.output())


# ---- fake clients ----------------------------------------------------------

class _FakeVision:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        VISION_CALL_LOG.append(request)
        # Pull the page/fig number from the user text to make the
        # description specific (so we can grep for it later).
        ut = request.messages[0].content[0].text  # type: ignore[index, attr-defined]
        return ChatResponse(
            text=f"Synthetic figure description for: {ut[:80]}",
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=200, output_tokens=40, cache_read_tokens=150),
            parsed_json=None,
        )


class _FakeIngest:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        INGEST_CALL_LOG.append(request)
        payload = {
            "summary": "PDF on Raft and Paxos with figures.",
            "description": {
                "sections": [
                    {"id": "s1", "title": "Introduction",
                     "anchor": {"unit": "pages", "value": "1-1"},
                     "summary": "Intro with figure.", "key_terms": ["raft"]},
                    {"id": "s2", "title": "Pipeline",
                     "anchor": {"unit": "pages", "value": "2-2"},
                     "summary": "Pipeline with figure.", "key_terms": ["paxos"]},
                    {"id": "s3", "title": "Conclusion",
                     "anchor": {"unit": "pages", "value": "3-3"},
                     "summary": "Conclusion.", "key_terms": ["wrap"]},
                ],
            },
            "kind": "text", "extra": "", "entry_extra": "",
            "entry_catalog_path": ["Research"],
            "entry_tags": [
                {"name": "consensus", "facet": "topic"},
                {"name": "pdf", "facet": "form"},
            ],
        }
        return ChatResponse(
            text=json.dumps(payload),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2500, output_tokens=400, cache_read_tokens=2000),
            parsed_json=payload,
        )


def _install_fakes() -> None:
    llm.reset_clients_cache()
    vision = _FakeVision()
    ingest = _FakeIngest()
    import marginalia.pipelines.pdf as pmod
    # PDF image extraction + VLM description live in pdf.py too. Patch
    # `get_chat_client` once: the fake decides by profile name. Ingest
    # path asks for "ingest"; image-describer asks for "vision".

    def _pick_client(profile: str = "ingest"):
        if profile == "vision":
            return vision
        return ingest
    pmod.get_chat_client = _pick_client  # type: ignore


# ---- helpers ---------------------------------------------------------------

async def _create_schema():
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _wait_for_done(file_id: str, timeout: float = 12.0) -> str:
    factory = get_session_factory()
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        async with factory() as s:
            row = (
                await s.execute(text(
                    "SELECT ingest_status FROM files WHERE id=:id"
                ), {"id": file_id})
            ).first()
            if row is None:
                raise RuntimeError("file vanished")
            (status,) = row
            if status in ("done", "failed", "dead"):
                return status
        await asyncio.sleep(0.1)
    raise TimeoutError("ingest did not finish")


async def main():
    _install_fakes()
    await _create_schema()
    pdf_bytes = _build_pdf_with_images()
    print("[setup] PDF size:", len(pdf_bytes), "bytes")

    runner = TaskRunner()
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        await runner.start()
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                r = await c.post(
                    "/v1/upload",
                    params={"remote_path": "/papers/"},
                    files={"file": ("paper.pdf", io.BytesIO(pdf_bytes),
                                    "application/pdf")},
                )
                assert r.status_code == 201, r.text
                up = r.json()
                file_id = up["file_id"]
                entry_id = up["entry_id"]

                status = await _wait_for_done(file_id)
                assert status == "done", f"ingest failed: {status}"
                print("[upload+ingest] OK; file =", file_id[:8])
        finally:
            await runner.stop()

    # ---- 1. vision was called for each significant figure ----------------
    # Big PNGs on pages 1 and 2 → 2 calls. Icon filtered.
    print(f"[1] vision calls: {len(VISION_CALL_LOG)}")
    assert len(VISION_CALL_LOG) == 2, \
        f"expected 2 vision calls (icon filtered), got {len(VISION_CALL_LOG)}"

    # Each call has exactly one TextBlock + one ImageBlock with png MIME
    for vc in VISION_CALL_LOG:
        blocks = vc.messages[0].content
        assert isinstance(blocks, list)
        text_blocks = [b for b in blocks if isinstance(b, TextBlock)]
        image_blocks = [b for b in blocks if isinstance(b, ImageBlock)]
        assert len(text_blocks) == 1 and len(image_blocks) == 1
        ib = image_blocks[0]
        assert ib.media_type in ("image/png", "image/jpeg")
        assert ib.data_b64
    print("[1] each vision call carries 1 TextBlock + 1 ImageBlock OK")

    # ---- 2. ingest call has [Figure X.Y] inserted into prompt -----------
    assert len(INGEST_CALL_LOG) == 1
    prompt = INGEST_CALL_LOG[0].messages[0].content[0].text  # type: ignore[index, attr-defined]
    assert "[Figure 1.1]" in prompt, "page 1 figure label missing"
    assert "[Figure 2.1]" in prompt, "page 2 figure label missing"
    # icon should NOT have a figure label
    assert "[Figure 1.2]" not in prompt, "icon was incorrectly described"
    # Each label is followed by a description
    assert "Synthetic figure description for" in prompt
    print("[2] ingest prompt has both figure labels + descriptions")

    # ---- 3. DB invariants ------------------------------------------------
    factory = get_session_factory()
    async with factory() as s:
        f = await s.get(File, file_id)
        assert f.kind == "text"
        assert isinstance(f.description, dict)
        sections = f.description["sections"]
        assert len(sections) == 3
        for sec in sections:
            assert sec["anchor"]["unit"] == "pages"
        print("[3] DB description.sections has 3 page-anchored sections")

    print("\nALL PDF_WITH_IMAGES E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
