"""Vision helpers for images embedded in document-shaped files."""
from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from marginalia.config import Settings, has_vision_profile
from marginalia.llm import ChatMessage, ChatRequest, ImageBlock, TextBlock, get_chat_client
from marginalia.llm.model_controls import DISABLE_THINKING_EXTRA_BODY
from marginalia.llm.tagged_response import strip_reasoning_text
from marginalia.pipelines.base import SegmentResult
from marginalia.pipelines.image import downscale_for_vlm
from marginalia.pipelines.text import TextPipeline

log = logging.getLogger(__name__)

MAX_DOCUMENT_IMAGE_BYTES = 20 * 1024 * 1024
MIN_DOCUMENT_IMAGE_BYTES = 2 * 1024
MIN_DOCUMENT_IMAGE_DIMENSION = 32
MIN_DOCUMENT_IMAGE_AREA = 4096
MAX_DOCUMENT_VISION_IMAGES = 20
MAX_DOCUMENT_QUESTION_IMAGES = 5

_DIRECT_VISION_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@dataclass(frozen=True, slots=True)
class DocumentImage:
    image_id: str
    label: str
    image_bytes: bytes
    media_type: str | None
    anchor: dict[str, object]
    context: str = ""
    filename: str | None = None


async def describe_document_images(
    *,
    settings: Settings,
    images: list[DocumentImage],
    document_name: str,
    document_kind: str,
    max_images: int | None = None,
) -> dict[str, object] | None:
    if not _document_vision_enabled(settings) or not images or not has_vision_profile(settings):
        return None
    effective_max_images = _settings_int(
        settings,
        "document_vision_max_images",
        MAX_DOCUMENT_VISION_IMAGES,
        lower=0,
    )
    if max_images is not None:
        effective_max_images = max(0, int(max_images))
    if effective_max_images <= 0:
        return None

    descriptions: list[dict[str, object]] = []
    skipped_small = 0
    for image in images[:effective_max_images]:
        prepared = _prepare_image_for_vision(
            image.image_bytes,
            image.media_type,
            settings=settings,
        )
        if prepared is None:
            if _is_likely_tiny_image(image.image_bytes, settings=settings):
                skipped_small += 1
            continue
        image_bytes, media_type = prepared
        try:
            text = await _complete_document_vision(
                settings=settings,
                system=(
                    "You are Marginalia's document vision miner. Extract readable "
                    "visible text and describe business-relevant diagrams, charts, "
                    "screenshots, forms, stamps, or signatures. If the image is "
                    "purely decorative or has no useful visible evidence, return "
                    "an empty string."
                ),
                prompt=_document_image_prompt(
                    document_name=document_name,
                    document_kind=document_kind,
                    image=image,
                    question=None,
                ),
                image_bytes=image_bytes,
                media_type=media_type,
                max_tokens=1024,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s embedded image vision failed for %s: %s",
                document_kind,
                image.label,
                exc,
            )
            continue
        if not text:
            continue
        descriptions.append(
            {
                "image_id": image.image_id,
                "label": image.label,
                "filename": image.filename,
                "media_type": media_type,
                "anchor": image.anchor,
                "context": image.context[:1200],
                "text": text,
                "text_chars": len(text),
            }
        )
    if not descriptions:
        return None
    text = render_document_image_descriptions(descriptions)
    return {
        "source": "pipeline_document_images",
        "document_kind": document_kind,
        "image_count": len(images),
        "images_indexed": len(descriptions),
        "images_skipped_small": skipped_small,
        "max_images": effective_max_images,
        "images": descriptions,
        "text": text,
        "text_chars": len(text),
    }


def append_document_image_vision_text(body: str, payload: dict[str, object] | None) -> str:
    if not payload:
        return body
    text = str(payload.get("text") or "").strip()
    if not text:
        return body
    base = (body or "").strip()
    if not base:
        return text
    return f"{base}\n\n--- Document image vision ---\n{text}"


def inline_document_image_vision_text(
    units: list[str],
    payload: dict[str, object] | None,
    *,
    anchor_key: str,
    title: str = "Embedded image vision",
) -> list[str]:
    if not payload:
        return units
    raw_images = payload.get("images")
    if not isinstance(raw_images, list):
        text = str(payload.get("text") or "").strip()
        return [append_document_image_vision_text("\n\n".join(units), payload)] if text else units

    grouped: dict[int, list[str]] = {}
    tail: list[str] = []
    for raw_image in raw_images:
        if not isinstance(raw_image, dict):
            continue
        text = _render_document_image_description(raw_image, include_context=False)
        if not text:
            continue
        anchor = raw_image.get("anchor")
        position = _anchor_int(anchor if isinstance(anchor, dict) else {}, anchor_key)
        if position <= 0:
            tail.append(text)
        else:
            grouped.setdefault(position, []).append(text)

    if not grouped and not tail:
        return units
    if not units:
        all_text = "\n\n".join(
            snippet
            for _position, snippets in sorted(grouped.items())
            for snippet in snippets
        )
        if tail:
            all_text = "\n\n".join(part for part in (all_text, "\n\n".join(tail)) if part)
        return [f"--- {title} ---\n{all_text}"] if all_text else units

    out = list(units)
    for position, snippets in sorted(grouped.items()):
        if position > len(out):
            tail.extend(snippets)
            continue
        unit = out[position - 1].rstrip()
        vision_text = "\n\n".join(snippets)
        out[position - 1] = (
            f"{unit}\n\n--- {title} ---\n{vision_text}"
            if unit
            else f"--- {title} ---\n{vision_text}"
        )
    if tail:
        out.append(f"--- {title} ---\n" + "\n\n".join(tail))
    return out


def document_vision_coverage(payload: dict[str, object] | None) -> dict[str, object] | None:
    if not payload:
        return None
    return {
        "source": payload.get("source"),
        "document_kind": payload.get("document_kind"),
        "image_count": payload.get("image_count"),
        "images_indexed": payload.get("images_indexed"),
        "images_skipped_small": payload.get("images_skipped_small"),
        "max_images": payload.get("max_images"),
    }


def attach_document_vision_description(
    description: dict[str, Any],
    payload: dict[str, object] | None,
) -> dict[str, Any]:
    if not payload:
        return description
    next_description = dict(description)
    next_description["document_vision"] = payload
    return next_description


async def answer_document_image_question(
    *,
    settings: Settings,
    images: list[DocumentImage],
    args: dict[str, Any],
    document_name: str,
    document_kind: str,
    mode: str,
) -> SegmentResult:
    question = str(args.get("question") or "").strip()
    if not question:
        return SegmentResult(error="question is required", extras={"mode": mode})
    if not _document_vision_enabled(settings):
        return SegmentResult(
            error="document vision is disabled",
            extras={"mode": mode, "question": question},
        )
    if not has_vision_profile(settings):
        return SegmentResult(
            error="vision profile is not configured",
            extras={"mode": mode, "question": question},
        )
    selected = select_document_images(
        images,
        args,
        limit=_settings_int(
            settings,
            "document_vision_question_max_images",
            MAX_DOCUMENT_QUESTION_IMAGES,
            lower=1,
        ),
    )
    if not selected:
        return SegmentResult(
            error="no document images matched the requested scope",
            extras={"mode": mode, "question": question, "image_count": len(images)},
        )

    answers: list[str] = []
    usage: list[dict[str, object]] = []
    skipped_small = 0
    for image in selected:
        prepared = _prepare_image_for_vision(
            image.image_bytes,
            image.media_type,
            settings=settings,
        )
        if prepared is None:
            if _is_likely_tiny_image(image.image_bytes, settings=settings):
                skipped_small += 1
            continue
        image_bytes, media_type = prepared
        try:
            text = await _complete_document_vision(
                settings=settings,
                system=(
                    "Answer the user's question using only the visible evidence "
                    "in this embedded document image. Include visible text "
                    "verbatim when relevant."
                ),
                prompt=_document_image_prompt(
                    document_name=document_name,
                    document_kind=document_kind,
                    image=image,
                    question=question,
                ),
                image_bytes=image_bytes,
                media_type=media_type,
                max_tokens=1024,
            )
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(
                error=f"VLM call failed: {exc}",
                extras={"mode": mode, "question": question},
            )
        if text:
            answers.append(f"[{image.label}]\n{text}")
        usage.append(
            {
                "image_id": image.image_id,
                "label": image.label,
                "anchor": image.anchor,
                "media_type": media_type,
            }
        )

    extras: dict[str, object] = {
        "mode": mode,
        "question": question,
        "image_count": len(images),
        "selected_images": [
            {
                "image_id": image.image_id,
                "label": image.label,
                "anchor": image.anchor,
            }
            for image in selected
        ],
        "vision": usage,
    }
    if skipped_small:
        extras["images_skipped_small"] = skipped_small
    if len(selected) < len(images):
        extras["scoped"] = True
    if not answers:
        return SegmentResult(text="", error="vision model returned no image answer", extras=extras)
    return SegmentResult(text="\n\n".join(answers), extras=extras)


def select_document_images(
    images: list[DocumentImage],
    args: dict[str, Any],
    *,
    limit: int = MAX_DOCUMENT_QUESTION_IMAGES,
) -> list[DocumentImage]:
    if not images:
        return []
    by_id = str(args.get("image_id") or "").strip()
    if by_id:
        return [image for image in images if image.image_id == by_id][:1]
    try:
        image_index = int(args.get("image_index"))
    except (TypeError, ValueError):
        image_index = 0
    if image_index > 0:
        return images[image_index - 1:image_index]

    slide_window = _range_from_args(args, "slide_start", "slide_end", "page_start", "page_end")
    if slide_window is not None:
        start, end = slide_window
        scoped = [
            image
            for image in images
            if _anchor_int(image.anchor, "slide") in range(start, end + 1)
        ]
        if scoped:
            return scoped[:limit]

    page_window = _range_from_args(args, "page_start", "page_end")
    if page_window is not None:
        start, end = page_window
        scoped = [
            image
            for image in images
            if _anchor_int(image.anchor, "page") in range(start, end + 1)
        ]
        if scoped:
            return scoped[:limit]

    block_window = _range_from_args(args, "paragraph_start", "paragraph_end")
    if block_window is not None:
        start, end = block_window
        scoped = [
            image
            for image in images
            if _anchor_int(image.anchor, "block") in range(start, end + 1)
        ]
        if scoped:
            return scoped[:limit]

    return images[:limit]


def persisted_document_image_segment(
    file_row: Any,
    args: dict[str, Any],
    *,
    mode: str,
    warning: str | None = None,
) -> SegmentResult:
    text = persisted_document_image_text(file_row)
    if not text:
        return SegmentResult(error="no persisted document image vision text", extras={"mode": mode})
    result = TextPipeline()._slice(body=text, args=args, file_row=None)
    result.extras.update({
        "source": "persisted_document_image_vision",
        "pipeline": "document_image_vision",
        "mode": mode,
        "answered_by": "persisted_document_image_vision",
    })
    if warning:
        result.extras["warning"] = warning
    return result


def persisted_document_image_text(file_row: Any) -> str:
    source = persisted_document_image_payload(file_row)
    if isinstance(source, dict):
        return str(source.get("text") or "").strip()
    return ""


def persisted_document_image_payload(file_row: Any) -> dict[str, object] | None:
    description = getattr(file_row, "description", None) or {}
    if not isinstance(description, dict):
        return None
    source = description.get("document_vision")
    return source if isinstance(source, dict) else None


def render_document_image_descriptions(images: list[dict[str, object]]) -> str:
    rendered: list[str] = []
    for image in images:
        rendered_image = _render_document_image_description(image, include_context=True)
        if rendered_image:
            rendered.append(rendered_image)
    return "\n\n".join(rendered)


def _render_document_image_description(
    image: dict[str, object],
    *,
    include_context: bool,
) -> str:
    text = str(image.get("text") or "").strip()
    if not text:
        return ""
    label = str(image.get("label") or image.get("image_id") or "Embedded image")
    anchor = image.get("anchor")
    anchor_text = _format_anchor(anchor if isinstance(anchor, dict) else {})
    context = str(image.get("context") or "").strip()
    lines = [f"[{label}{(' | ' + anchor_text) if anchor_text else ''}]"]
    if include_context and context:
        lines.append(f"Nearby text: {context[:500]}")
    lines.append(text)
    return "\n".join(lines)


async def _complete_document_vision(
    *,
    settings: Settings,
    system: str,
    prompt: str,
    image_bytes: bytes,
    media_type: str,
    max_tokens: int,
) -> str:
    del settings
    client = get_chat_client("vision")
    extra_body = (
        DISABLE_THINKING_EXTRA_BODY
        if getattr(client, "provider", None) == "openai-compatible"
        else None
    )
    response = await client.complete(
        ChatRequest(
            system=system,
            messages=[
                ChatMessage(
                    role="user",
                    content=[
                        TextBlock(text=prompt),
                        ImageBlock(
                            media_type=media_type,  # type: ignore[arg-type]
                            data_b64=base64.b64encode(image_bytes).decode("ascii"),
                        ),
                    ],
                )
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            extra_body=extra_body,
        )
    )
    return strip_reasoning_text(response.text).strip()


def _prepare_image_for_vision(
    image_bytes: bytes,
    media_type: str | None,
    *,
    settings: Settings,
) -> tuple[bytes, str] | None:
    if not image_bytes or len(image_bytes) > MAX_DOCUMENT_IMAGE_BYTES:
        return None
    if _is_likely_tiny_image(image_bytes, settings=settings):
        return None
    normalized = _normalize_media_type(media_type)
    if normalized in _DIRECT_VISION_MEDIA_TYPES:
        prepared = downscale_for_vlm(image_bytes)
        if prepared is not None:
            return prepared
        return image_bytes, normalized
    prepared = downscale_for_vlm(image_bytes)
    if prepared is not None:
        return prepared
    return _convert_image_to_png(image_bytes)


def _convert_image_to_png(image_bytes: bytes) -> tuple[bytes, str] | None:
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            image.load()
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGB")
            out = BytesIO()
            image.save(out, format="PNG", optimize=True)
            converted = out.getvalue()
    except Exception:
        return None
    if not converted or len(converted) > MAX_DOCUMENT_IMAGE_BYTES:
        return None
    return converted, "image/png"


def _is_likely_tiny_image(image_bytes: bytes, *, settings: Settings) -> bool:
    min_bytes = _settings_int(
        settings,
        "document_vision_min_image_bytes",
        MIN_DOCUMENT_IMAGE_BYTES,
        lower=0,
    )
    if len(image_bytes) < min_bytes:
        return True
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            width, height = image.size
    except Exception:
        return False
    if width <= 0 or height <= 0:
        return True
    min_dimension = _settings_int(
        settings,
        "document_vision_min_image_dimension",
        MIN_DOCUMENT_IMAGE_DIMENSION,
        lower=0,
    )
    min_area = _settings_int(
        settings,
        "document_vision_min_image_area",
        MIN_DOCUMENT_IMAGE_AREA,
        lower=0,
    )
    return width < min_dimension or height < min_dimension or (width * height) < min_area


def _document_vision_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "document_vision_enabled", True))


def _settings_int(settings: Settings, name: str, default: int, *, lower: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        value = default
    return max(lower, value)


def _normalize_media_type(media_type: str | None) -> str:
    clean = str(media_type or "").split(";", 1)[0].strip().lower()
    if clean == "image/jpg":
        return "image/jpeg"
    return clean


def _document_image_prompt(
    *,
    document_name: str,
    document_kind: str,
    image: DocumentImage,
    question: str | None,
) -> str:
    parts = [
        f"Document: {document_name or 'unknown'}",
        f"Document type: {document_kind}",
        f"Image: {image.label}",
        f"Position: {_format_anchor(image.anchor)}",
    ]
    if image.context.strip():
        parts.append(f"Nearby extracted text:\n{image.context[:1200]}")
    if question:
        parts.append(f"Question: {question}")
    else:
        parts.append(
            "Return concise Markdown with two fields when applicable: Visible "
            "text, and Visual summary. If there is no readable or useful "
            "visual evidence, return an empty string."
        )
    return "\n\n".join(part for part in parts if part)


def _format_anchor(anchor: dict[str, object]) -> str:
    if not anchor:
        return ""
    if anchor.get("slide"):
        return f"slide {anchor.get('slide')}"
    if anchor.get("page"):
        return f"page {anchor.get('page')}"
    if anchor.get("block"):
        return f"block {anchor.get('block')}"
    return ", ".join(f"{key}={value}" for key, value in anchor.items())


def _anchor_int(anchor: dict[str, object], key: str) -> int:
    try:
        return int(anchor.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _range_from_args(
    args: dict[str, Any],
    start_key: str,
    end_key: str,
    fallback_start_key: str | None = None,
    fallback_end_key: str | None = None,
) -> tuple[int, int] | None:
    start_raw = args.get(start_key)
    end_raw = args.get(end_key)
    if start_raw in (None, "") and fallback_start_key:
        start_raw = args.get(fallback_start_key)
    if end_raw in (None, "") and fallback_end_key:
        end_raw = args.get(fallback_end_key)
    if start_raw in (None, "") and end_raw in (None, ""):
        return None
    try:
        start = max(1, int(start_raw)) if start_raw not in (None, "") else 1
        end = int(end_raw) if end_raw not in (None, "") else start
    except (TypeError, ValueError):
        return None
    if end < start:
        return None
    return start, end
