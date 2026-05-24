"""Pipeline contract (DESIGN.md §11.2).

Pipelines are pure: they read bytes via storage, call the LLM, and
return a `PipelineResult`. They never touch the DB — the handler does
that, so the write-once rules and transaction are enforced in one
place.

A pipeline also knows how to **read back** a previously-ingested file:
`read_segment(file, locator, storage)` answers questions of the form
"give me section s3 of this file" or "give me lines 100-150". The
agent's `read_files` tool is a thin dispatcher — for each entry, it
resolves the appropriate pipeline by mime/ext and delegates.

Containers (zip / tar / mbox / git_repo) override `list_members` to
return their manifest. The same `read_segment` then accepts a
`member_path` locator and dispatches internally to a sub-pipeline for
that member's mime type. Non-container pipelines return None from
`list_members`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

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
    display_name: str | None = None   # original upload filename, for
                                      # pipelines like archive that need
                                      # the suffix to pick a decoder
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


@dataclass(slots=True)
class SegmentResult:
    """One segment of a file's body returned by `read_segment`.

    Success: `text` is the extracted content, `error` is None,
             `extras` may carry pipeline-specific metadata
             (e.g. `total_pages`, `next_offset`, `truncated`).
    Failure: `text` is empty, `error` carries a one-line reason; the
             agent's read_files tool surfaces this as ok=false.
    """
    text: str = ""
    error: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class Pipeline(ABC):
    """Concrete pipeline (text / pdf / image / docx / spreadsheet / log /
    container). All pipelines must support `run` (ingest); most also
    implement `read_segment` (read back). Containers additionally
    override `list_members`.

    `read_segment` accepts the full read_files args dict, not a single
    locator — different pipelines respond to different fields:

      generic              offset, max_chars, pattern, context_lines,
                           max_matches
      text-shaped          line_start / line_end, section_id, heading
      pdf                  page_start / page_end (+ generic chunking)
      docx                 paragraph_start / paragraph_end
      container            member_path (delegates to a sub-pipeline for
                           that member)

    A pipeline ignores fields it doesn't understand. If no usable field
    is set it returns the prefix (offset .. offset+max_chars) of the
    extracted body — that's the default chunked-read behaviour.
    """

    name: str

    @abstractmethod
    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult: ...

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Default — pipelines that haven't implemented this yet decline."""
        return SegmentResult(
            error=f"{self.name} pipeline does not support read_segment yet",
        )

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first read_segment. Used by ArchivePipeline to dispatch
        reads of an archive's internal members (which never become file
        rows of their own). Pipelines that don't override this decline.
        """
        return SegmentResult(
            error=f"{self.name} pipeline does not support "
                  "read_segment_from_bytes yet",
        )

    async def list_members(
        self,
        *,
        file_row: Any,
        storage: StorageBackend,
    ) -> list[dict[str, Any]] | None:
        """Containers override this. Returns None for non-containers."""
        return None
