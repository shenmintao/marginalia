"""Pipeline registry: route (mime, ext) → Pipeline.

Each pipeline self-registers via `@register_pipeline(...)`. The handler asks
`resolve_pipeline(mime, ext)`; the first matching registered entry wins.

Match precedence:
  1. exact mime match
  2. mime prefix match (e.g. "text/" matches "text/markdown")
  3. extension match (case-insensitive, with leading dot)
  4. fallback (a pipeline registered with `fallback=True`)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from marginalia.pipelines.base import Pipeline

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Registration:
    pipeline: Pipeline
    mimes: tuple[str, ...] = ()
    mime_prefixes: tuple[str, ...] = ()
    exts: tuple[str, ...] = ()
    fallback: bool = False


_REGISTRY: list[_Registration] = []


def register_pipeline(
    *,
    mimes: tuple[str, ...] = (),
    mime_prefixes: tuple[str, ...] = (),
    exts: tuple[str, ...] = (),
    fallback: bool = False,
) -> Callable[[type[Pipeline]], type[Pipeline]]:
    """Class decorator. Instantiates the pipeline (no-arg ctor) and registers it."""
    norm_exts = tuple(e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts)

    def decorator(cls: type[Pipeline]) -> type[Pipeline]:
        instance = cls()  # type: ignore[call-arg]
        _REGISTRY.append(
            _Registration(
                pipeline=instance,
                mimes=mimes,
                mime_prefixes=mime_prefixes,
                exts=norm_exts,
                fallback=fallback,
            )
        )
        log.debug("registered pipeline %s (mimes=%s exts=%s fallback=%s)",
                  instance.name, mimes, exts, fallback)
        return cls

    return decorator


def resolve_pipeline(mime: str | None, ext: str | None) -> Pipeline | None:
    mime = mime or ""
    ext_l = (ext or "").lower()
    if ext_l and not ext_l.startswith("."):
        ext_l = "." + ext_l

    # 1. exact mime
    for r in _REGISTRY:
        if mime and mime in r.mimes:
            return r.pipeline
    # 2. mime prefix
    for r in _REGISTRY:
        for prefix in r.mime_prefixes:
            if mime.startswith(prefix):
                return r.pipeline
    # 3. extension
    for r in _REGISTRY:
        if ext_l and ext_l in r.exts:
            return r.pipeline
    # 4. fallback
    for r in _REGISTRY:
        if r.fallback:
            return r.pipeline
    return None


def registered_pipelines() -> list[str]:
    return [r.pipeline.name for r in _REGISTRY]
