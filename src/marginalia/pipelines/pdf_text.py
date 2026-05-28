"""PDF text extraction, page-label mapping, and small in-process caches.

The ingest pipeline and read tools both need PDF text, but not at the same
granularity. Readback should not extract a 1000-page PDF just to return page
900; citation quote lookup does need whole-document text, so that path uses
an LRU cache keyed by immutable file content when possible.
"""
from __future__ import annotations

import io
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from marginalia.storage.base import StorageBackend

PDF_TEXT_CACHE_MAX_DOCS = 6
PDF_LABEL_CACHE_MAX_DOCS = 32


@dataclass(slots=True)
class PdfTextRange:
    pages: list[str]
    page_labels: list[str]
    page_start: int
    total_pages: int


_TEXT_CACHE: OrderedDict[str, PdfTextRange] = OrderedDict()
_LABEL_CACHE: OrderedDict[str, list[str]] = OrderedDict()


async def read_storage_bytes(storage: StorageBackend, key: str) -> bytes:
    buf = bytearray()
    async for chunk in storage.get(key):
        buf.extend(chunk)
    return bytes(buf)


def pdf_page_count(pdf_bytes: bytes) -> int:
    from pypdf import PdfReader

    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def extract_pdf_page_labels(pdf_bytes: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    try:
        labels = list(reader.page_labels or [])
    except Exception:  # noqa: BLE001
        labels = []
    if len(labels) < total:
        labels.extend(str(i) for i in range(len(labels) + 1, total + 1))
    return [str(label) if label is not None else str(i) for i, label in enumerate(labels[:total], start=1)]


def extract_pdf_text_range(
    pdf_bytes: bytes,
    *,
    page_start: int = 1,
    page_end: int | None = None,
) -> PdfTextRange:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    if total <= 0:
        return PdfTextRange(pages=[], page_labels=[], page_start=1, total_pages=0)
    start = max(1, min(int(page_start), total))
    end = total if page_end is None else max(start, min(int(page_end), total))
    pages: list[str] = []
    for page in reader.pages[start - 1:end]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            pages.append("")
    labels = _labels_from_reader(reader, total)[start - 1:end]
    return PdfTextRange(
        pages=pages,
        page_labels=labels,
        page_start=start,
        total_pages=total,
    )


async def get_pdf_text_for_file(
    storage: StorageBackend,
    file: Any,
) -> PdfTextRange:
    cache_key = _cache_key(file)
    if cache_key:
        cached = _get_lru(_TEXT_CACHE, cache_key)
        if cached is not None:
            return cached
    storage_key = getattr(file, "storage_key", None)
    if not storage_key:
        raise ValueError("file has no storage_key")
    pdf_bytes = await read_storage_bytes(storage, str(storage_key))
    doc = extract_pdf_text_range(pdf_bytes, page_start=1, page_end=None)
    if cache_key:
        _put_lru(_TEXT_CACHE, cache_key, doc, PDF_TEXT_CACHE_MAX_DOCS)
        _put_lru(_LABEL_CACHE, cache_key, doc.page_labels, PDF_LABEL_CACHE_MAX_DOCS)
    return doc


async def get_pdf_page_labels_for_file(
    storage: StorageBackend,
    file: Any,
) -> list[str]:
    cache_key = _cache_key(file)
    if cache_key:
        cached = _get_lru(_LABEL_CACHE, cache_key)
        if cached is not None:
            return cached
    storage_key = getattr(file, "storage_key", None)
    if not storage_key:
        return []
    pdf_bytes = await read_storage_bytes(storage, str(storage_key))
    labels = extract_pdf_page_labels(pdf_bytes)
    if cache_key:
        _put_lru(_LABEL_CACHE, cache_key, labels, PDF_LABEL_CACHE_MAX_DOCS)
    return labels


def locate_quote_page(doc: PdfTextRange, quote: str) -> int | None:
    needle = _unescape_quote(quote)
    norm_needle = _norm(needle)
    compact_needle = _compact(needle)
    if not norm_needle and not compact_needle:
        return None
    for idx, page_text in enumerate(doc.pages, start=doc.page_start):
        if norm_needle and norm_needle in _norm(page_text):
            return idx
        if compact_needle and compact_needle in _compact(page_text):
            return idx
    return None


def resolve_page_label(labels: list[str], value: str | int | None) -> int | None:
    first = first_page_number(value)
    if first is None:
        return None
    wanted = str(first)
    wanted_norm = _label_norm(wanted)
    matches = [
        idx for idx, label in enumerate(labels, start=1)
        if _label_norm(label) == wanted_norm
    ]
    if len(matches) == 1:
        return matches[0]
    if 1 <= first <= len(labels):
        return first
    return None


def first_page_number(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    if not m:
        return None
    try:
        n = int(m.group(0))
    except ValueError:
        return None
    return n if n > 0 else None


def render_pdf_text_pages(doc: PdfTextRange) -> str:
    chunks: list[str] = []
    for offset, txt in enumerate(doc.pages):
        page = doc.page_start + offset
        label = doc.page_labels[offset] if offset < len(doc.page_labels) else str(page)
        label_line = "" if label == str(page) else f"\n[Page label: {label}]"
        chunks.append(f"[Page {page}]{label_line}\n{txt}")
    return "\n\n".join(chunks)


def _labels_from_reader(reader: Any, total: int) -> list[str]:
    try:
        labels = list(reader.page_labels or [])
    except Exception:  # noqa: BLE001
        labels = []
    if len(labels) < total:
        labels.extend(str(i) for i in range(len(labels) + 1, total + 1))
    return [
        str(label) if label is not None else str(i)
        for i, label in enumerate(labels[:total], start=1)
    ]


def _cache_key(file: Any) -> str | None:
    for attr in ("sha256", "id", "storage_key"):
        value = getattr(file, attr, None)
        if value:
            return f"{attr}:{value}"
    return None


def _get_lru(cache: OrderedDict[str, Any], key: str) -> Any | None:
    try:
        value = cache.pop(key)
    except KeyError:
        return None
    cache[key] = value
    return value


def _put_lru(cache: OrderedDict[str, Any], key: str, value: Any, max_items: int) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_items:
        cache.popitem(last=False)


def _unescape_quote(s: str) -> str:
    return s.replace(r"\"", '"').replace(r"\\", "\\")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _label_norm(text: str) -> str:
    return str(text).strip().casefold()
