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

import os
from uuid import uuid4
import asyncio
import io
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

_TEST_PARENT = Path(os.environ.get("MARGINALIA_TEST_TMP", Path(__file__).resolve().parent))
_TEST_ROOT = _TEST_PARENT / f"_pdf_with_images_e2e_data_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True)
os.environ["MARGINALIA_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"
os.environ["LLM_VISION_MODEL"] = "fake-vision"

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


def _request_text(request: ChatRequest) -> str:
    parts: list[str] = []
    for msg in request.messages:
        if isinstance(msg.content, str):
            parts.append(msg.content)
        else:
            parts.extend(getattr(block, "text", "") for block in msg.content)
    return "\n".join(p for p in parts if p)


def _rgb_image_data(
    w: int,
    h: int,
    rgb: tuple[int, int, int] = (40, 90, 200),
    noisy: bool = True,
) -> bytes:
    raw = bytearray()
    if noisy:
        # deterministic pseudo-random per pixel
        seed = (rgb[0] * 31 + rgb[1] * 53 + rgb[2] * 97) & 0xFFFFFFFF
        for y in range(h):
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
        raw = bytearray(bytes(rgb) * w * h)
    return bytes(raw)


def _pdf_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")


def _image_xobject(
    writer,
    *,
    w: int,
    h: int,
    rgb: tuple[int, int, int],
    noisy: bool = True,
):
    from pypdf.generic import EncodedStreamObject, NameObject, NumberObject

    image = EncodedStreamObject()
    image._data = zlib.compress(_rgb_image_data(w, h, rgb, noisy=noisy))
    image.update({
        NameObject("/Filter"): NameObject("/FlateDecode"),
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(w),
        NameObject("/Height"): NumberObject(h),
        NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
        NameObject("/BitsPerComponent"): NumberObject(8),
    })
    return writer._add_object(image)


def _add_pdf_page(writer, *, text: str, images: list[dict[str, object]]) -> None:
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    xobjects = DictionaryObject()
    ops = [f"BT /F1 11 Tf 1 0 0 1 40 250 Tm ({_pdf_text(text)}) Tj ET"]
    for idx, spec in enumerate(images, start=1):
        name = NameObject(f"/Im{idx}")
        xobjects[name] = _image_xobject(
            writer,
            w=int(spec["pixels_w"]),
            h=int(spec["pixels_h"]),
            rgb=spec["rgb"],  # type: ignore[arg-type]
            noisy=bool(spec.get("noisy", True)),
        )
        ops.append(
            f"q {spec['draw_w']} 0 0 {spec['draw_h']} "
            f"{spec['x']} {spec['y']} cm {name} Do Q"
        )
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        NameObject("/XObject"): xobjects,
    })
    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("ascii"))
    page.replace_contents(stream)


def _build_pdf_with_images() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    _add_pdf_page(
        writer,
        text=(
            "Page 1 Introduction discusses Raft consensus leader election "
            "log replication and safety properties with a timing diagram."
        ),
        images=[
            {
                "pixels_w": 220,
                "pixels_h": 160,
                "rgb": (200, 80, 50),
                "x": 10,
                "y": 70,
                "draw_w": 80,
                "draw_h": 60,
            },
            {
                "pixels_w": 20,
                "pixels_h": 20,
                "rgb": (255, 255, 255),
                "x": 100,
                "y": 70,
                "draw_w": 8,
                "draw_h": 8,
                "noisy": False,
            },
        ],
    )
    _add_pdf_page(
        writer,
        text=(
            "Page 2 Pipeline describes Paxos majority quorum consensus "
            "with proposers acceptors learners prepare and accept messages."
        ),
        images=[
            {
                "pixels_w": 180,
                "pixels_h": 120,
                "rgb": (50, 180, 100),
                "x": 10,
                "y": 70,
                "draw_w": 70,
                "draw_h": 50,
            },
        ],
    )
    _add_pdf_page(
        writer,
        text=(
            "Page 3 Conclusion compares Raft and Paxos across ergonomics "
            "performance and pedagogical clarity."
        ),
        images=[],
    )
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ---- fake clients ----------------------------------------------------------

class _FakeVision:
    profile_name = "vision"
    model = "fake-vision"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        VISION_CALL_LOG.append(request)
        # Pull the page/fig number from the user text to make the
        # description specific (so we can grep for it later).
        ut = _request_text(request)
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
        tagged = """<summary>
PDF on Raft and Paxos with figures.
</summary>
<description>
The PDF includes consensus content and extracted figure descriptions.
</description>
<sections>
s1 | pages 1-1 | Introduction | Intro with figure. | raft
s2 | pages 2-2 | Pipeline | Pipeline with figure. | paxos
s3 | pages 3-3 | Conclusion | Conclusion. | wrap
</sections>
<extra>
</extra>
<entry_extra>
</entry_extra>
<catalog_path>Research</catalog_path>
<tags>
topic: consensus
form: pdf
</tags>"""
        return ChatResponse(
            text=tagged,
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=2500, output_tokens=400, cache_read_tokens=2000),
            parsed_json=None,
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
    import marginalia.tasks.handlers.periodic_tick as tickmod

    async def _no_periodic_bootstrap() -> None:
        return None

    tickmod.bootstrap_periodic_tick = _no_periodic_bootstrap  # type: ignore[assignment]


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
    prompt = _request_text(INGEST_CALL_LOG[0])
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
        figures = f.description.get("figures") or []
        assert len(figures) == 2
        assert figures[0]["label"].startswith("Figure ")
        assert "Synthetic figure description for" in figures[0]["text"]
        print("[3] DB description.sections has 3 page-anchored sections")

    print("\nALL PDF_WITH_IMAGES E2E CHECKS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        sys.exit(1)
