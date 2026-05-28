"""Unit tests for the read_segment VLM-on-read dispatch.

These do NOT spin up the full app — they construct a pipeline, hand it
a SimpleNamespace standing in for a File row, and assert that the
correct branch fired (VLM call vs persisted-text/OCR fallback vs error).
The vision client is patched to a lambda that returns a canned answer
without touching the network.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from marginalia.llm.types import ChatResponse, TokenUsage
from marginalia.pipelines.image import ImagePipeline
from marginalia.pipelines.pdf import PdfPipeline


class _FakeStorage:
    """Minimal StorageBackend stub. Yields fixed bytes on get()."""

    def __init__(self, payload: bytes):
        self._payload = payload

    async def get(self, key: str):  # noqa: ARG002
        yield self._payload


class _FakeVisionClient:
    def __init__(self, text: str):
        self._text = text
        self.calls: list = []

    async def complete(self, request):
        self.calls.append(request)
        return ChatResponse(
            text=self._text,
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=0, output_tokens=0, cache_read_tokens=0),
        )


# A 1-byte payload is enough — downscale_for_vlm runs through Pillow
# but on any decode failure falls back to returning the original bytes
# as image/png, which is fine for our purposes (we just need *something*
# to go into the ImageBlock).
_TINY_IMAGE_BYTES = b"x"


def test_image_with_question_calls_vlm(monkeypatch):
    fake = _FakeVisionClient(text="this is a cat")
    monkeypatch.setattr(
        "marginalia.pipelines.image.has_vision_profile", lambda: True,
    )
    monkeypatch.setattr(
        "marginalia.pipelines.image.get_chat_client", lambda _name: fake,
    )

    pipeline = ImagePipeline()
    file_row = SimpleNamespace(
        storage_key="any",
        summary="cat photo",
        description={},
    )
    result = asyncio.run(pipeline.read_segment(
        file_row=file_row,
        args={"question": "what animal is in the picture?"},
        storage=_FakeStorage(_TINY_IMAGE_BYTES),
    ))
    assert result.error is None
    assert result.text == "this is a cat"
    assert result.extras["vlm_used"] is True
    assert len(fake.calls) == 1
    # The user message must include both a text block AND an image block.
    blocks = fake.calls[0].messages[0].content
    types = [type(b).__name__ for b in blocks]
    assert "TextBlock" in types and "ImageBlock" in types


def test_image_without_question_returns_persisted_description():
    pipeline = ImagePipeline()
    file_row = SimpleNamespace(
        storage_key="any",
        summary="a cat sitting on a mat",
        description={},
    )
    result = asyncio.run(pipeline.read_segment(
        file_row=file_row, args={}, storage=_FakeStorage(b""),
    ))
    assert result.error is None
    assert "cat sitting on a mat" in result.text
    # No vlm_used flag — we never called the VLM.
    assert result.extras.get("vlm_used") is not True


def test_ocr_pdf_without_question_reads_stored_text():
    pipeline = PdfPipeline()
    file_row = SimpleNamespace(
        storage_key="any",
        description={
            "ocr": {
                "engine": "vlm",
                "pages_total": 3,
                "pages_processed": 1,
                "document_type": "book",
                "stored_pages": 1,
            },
            "ocr_pages": [{
                "page": 1,
                "text": "Raft Consensus\nLeader election uses randomized timers.",
                "blocks": [],
            }],
        },
    )
    result = asyncio.run(pipeline.read_segment(
        file_row=file_row,
        args={"pattern": "Leader election"},
        storage=_FakeStorage(b""),
    ))
    assert result.error is None
    assert "Leader election" in result.text
    assert result.extras.get("ocr_indexed") is True
    assert result.extras.get("ocr_document_type") == "book"
    assert result.extras.get("ocr_pages_total") == 3
    assert result.extras.get("ocr_pages_processed") == 1
    assert result.extras.get("ocr_stored_pages") == 1
    assert result.extras.get("total_pages") == 3


def test_text_pdf_ignores_ocr_branch():
    """A regular text-layer PDF shouldn't get routed through VLM even
    if `question` is set — the branch is only taken when description.ocr
    is present."""
    pipeline = PdfPipeline()
    file_row = SimpleNamespace(storage_key="any", description={})
    # The slice path will fail with "PDF parse failed" since we hand it
    # garbage bytes — that's enough to prove we took the slice branch
    # and not the VLM branch.
    result = asyncio.run(pipeline.read_segment(
        file_row=file_row,
        args={"question": "ignored here"},
        storage=_FakeStorage(b"not a pdf"),
    ))
    assert result.error is not None
    assert "PDF parse failed" in result.error
