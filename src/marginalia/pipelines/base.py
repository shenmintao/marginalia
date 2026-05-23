"""Pipeline contract (design.md §11.2).

Pipelines are pure: they read bytes via storage, call the LLM, and return a
`PipelineResult`. They never touch the DB — the handler does that, so the
write-once rules and transaction are enforced in one place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from marginalia.storage.base import StorageBackend


@dataclass(slots=True)
class TagSuggestion:
    """A tag the pipeline wants to attach. Resolved by the handler against
    the current `tags` table (existing → reuse id; new → INSERT a row)."""
    name: str
    facet: str  # topic | form | time | source | language | extra


@dataclass(slots=True)
class PipelineContext:
    """Inputs handed to a pipeline. Hints (folder path, sibling names, catalog
    sketch, tag vocabulary) are advisory — the LLM uses them as priors but is
    not bound by them."""
    file_id: str
    storage_key: str
    sha256: str
    size_bytes: int
    mime_type: str | None
    original_ext: str | None
    folder_path: str          # e.g. "/research/llm" — display only
    sibling_names: list[str]  # other entries in the same folder
    catalog_sketch: list[dict[str, Any]] = field(default_factory=list)
    tag_vocabulary: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class PipelineResult:
    """One pipeline call's full output. The handler fans this out to the DB."""
    # files.* (write-once, content-only)
    summary: str
    description: dict[str, Any]
    kind: str
    extra: str | None

    # entry.* (per-position fields, mutable after first write)
    entry_extra: str | None
    entry_catalog_path: list[str] | None  # ['Research','LLM'] — handler resolves to id
    entry_tags: list[TagSuggestion] = field(default_factory=list)


@runtime_checkable
class Pipeline(Protocol):
    """One concrete pipeline (text / code / pdf / ...).

    Implementations may stream from `storage` themselves or use the helper
    `read_all_text` from `pipelines.utils`.
    """

    name: str

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult: ...
