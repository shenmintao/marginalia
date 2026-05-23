"""Pipeline framework: contract + registry + first concrete (text).

Per design.md §11.2 each pipeline produces, from one LLM call:
  - files.summary   (write-once, content-only)
  - files.description  (write-once, structured JSON)
  - files.kind      (write-once, content-only)
  - files.extra     (write-once, content-only)
  - the entry's catalog_id (best-guess placement; restructure may move later)
  - the entry's entry_tags (chosen against current vocabulary, may create new)
  - the entry's extra (position-aware insight)

The pipeline does NOT touch the DB. It returns a `PipelineResult`; the
ingest_file handler is responsible for persistence (write-once locking,
audit events, tag resolution, transaction).
"""
from marginalia.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    TagSuggestion,
)
from marginalia.pipelines.registry import (
    register_pipeline,
    registered_pipelines,
    resolve_pipeline,
)

__all__ = [
    "Pipeline",
    "PipelineContext",
    "PipelineResult",
    "TagSuggestion",
    "register_pipeline",
    "registered_pipelines",
    "resolve_pipeline",
]

# Importing the concrete pipelines registers them via decorators.
from marginalia.pipelines import container  # noqa: E402, F401
from marginalia.pipelines import image  # noqa: E402, F401
from marginalia.pipelines import pdf  # noqa: E402, F401
from marginalia.pipelines import text  # noqa: E402, F401
