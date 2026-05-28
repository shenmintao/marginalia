"""Helpers for recall-style text query arguments."""
from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from typing import Any


_SEPARATOR_RE = re.compile(r"[\s,，、;；]+")


def normalize_text_queries(value: Any) -> list[str]:
    """Return OR-style text query terms from a tool argument.

    `text=["raft", "paxos"]` is explicit. For older or sloppier calls that
    pass `text="raft paxos"`, split on whitespace and common list separators.
    Quoted phrases in a string stay together via shlex.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return _dedupe(_split_string_query(value))
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        terms = [str(item).strip() for item in value if str(item).strip()]
        return _dedupe(terms)
    text = str(value).strip()
    return [text] if text else []


def _split_string_query(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if '"' in stripped or "'" in stripped:
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = []
        if parts:
            return [_clean_part(part) for part in parts if _clean_part(part)]
    return [
        _clean_part(part)
        for part in _SEPARATOR_RE.split(stripped)
        if _clean_part(part)
    ]


def _clean_part(text: str) -> str:
    return text.strip(" \t\r\n,，、;；")


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        key = item.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
