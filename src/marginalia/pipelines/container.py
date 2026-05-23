"""Container pipeline (design.md §11.4).

Treats zip / tar / git archives as a single file row. Does NOT create
inner leaf entries — internal exploration happens at agent time via the
`analyze_container` tool.

Output:
  files.kind = 'container'
  files.description = {
    container_kind, file_count, total_uncompressed_bytes,
    primary_language?, frameworks_detected?, tree, indexed_files,
    key_files (with summaries), ingest_filters_applied
  }

LLM call shape: ONE chat completion that sees:
  - the directory tree
  - up to N key files (full text, capped per file)
  - the ingest profile schema (CONTAINER_PIPELINE_SCHEMA)
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from marginalia.llm import (
    ChatMessage, ChatRequest, TextBlock, get_chat_client,
)
from marginalia.pipelines.base import (
    Pipeline, PipelineContext, PipelineResult, TagSuggestion,
)
from marginalia.pipelines.container_extract import (
    KEY_FILE_PATTERNS, detect_kind, directory_tree, extract, pick_key_files,
)
from marginalia.pipelines.registry import register_pipeline
from marginalia.storage.base import StorageBackend

log = logging.getLogger(__name__)


KEY_FILE_BYTES_LIMIT = 8 * 1024  # max body shown per key_file
KEY_FILES_MAX = 6


CONTAINER_PIPELINE_SYSTEM = """You are Marginalia's container indexer.

You are given a directory tree summary + a few key files' full text from
a software repository or archive. Produce a structured index that lets a
downstream agent decide whether to retrieve the container and find the
relevant inner file.

Rules:
- Output ONLY one JSON object matching the provided schema.
- `summary`: 2-4 sentences in the dominant language describing what the
  container is and what it contains.
- `description.primary_language`: detected from the file mix (only if
  the container looks like a code repo; otherwise null).
- `description.frameworks_detected`: short list, evidence-based.
- `kind`: "container".
- `extra`: at most 1 paragraph of cross-cutting insight; "" if nothing.
- `entry_extra`: at most 1 paragraph of position-aware insight; "" if none.
- `entry_catalog_path`: best-guess classification path as a list of names.
- `entry_tags`: 3-10 tags. Each `{name, facet}` with facets:
  topic | form | time | source | language | extra.

Do NOT speculate beyond what is visible. The tree and key files are the
only ground truth.
"""


CONTAINER_PIPELINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "description", "kind", "extra",
                 "entry_extra", "entry_catalog_path", "entry_tags"],
    "properties": {
        "summary": {"type": "string"},
        "description": {
            "type": "object",
            "additionalProperties": False,
            "required": ["primary_language", "frameworks_detected"],
            "properties": {
                "primary_language": {"type": ["string", "null"]},
                "frameworks_detected": {
                    "type": "array", "items": {"type": "string"},
                },
            },
        },
        "kind": {"type": "string", "enum": ["container"]},
        "extra": {"type": "string"},
        "entry_extra": {"type": "string"},
        "entry_catalog_path": {"type": "array", "items": {"type": "string"}},
        "entry_tags": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "facet"],
                "properties": {
                    "name": {"type": "string"},
                    "facet": {"type": "string", "enum": [
                        "topic", "form", "time", "source",
                        "language", "extra",
                    ]},
                },
            },
        },
    },
}


@register_pipeline(
    mimes=("application/zip", "application/x-tar", "application/gzip",
           "application/x-gzip", "application/x-bzip2"),
    exts=(".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2"),
)
class ContainerPipeline(Pipeline):
    name = "container"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        body = await self._read_bytes(storage, ctx.storage_key)

        tmpdir = Path(tempfile.mkdtemp(prefix="marg-container-"))
        try:
            extracted = extract(body, extract_root=tmpdir)
            members = extracted.members
            total_bytes = sum(m.size for m in members)

            tree = directory_tree(members)
            indexed_files = [
                {"path": m.path, "size": m.size}
                for m in members[:200]   # cap for prompt budget
            ]
            key_files = pick_key_files(members, limit=KEY_FILES_MAX)

            # If this is a git repository, parse the inner .git tree —
            # branch / recent commits / authors. Best-effort enrichment;
            # parse() returns None if .git/ isn't there.
            git_meta = None
            if extracted.container_kind == "git_repo":
                from marginalia.pipelines.git_metadata import parse as parse_git
                git_meta = parse_git(tmpdir)
            key_files_payload: list[dict[str, Any]] = []
            for m in key_files:
                src_path = tmpdir / m.path
                try:
                    raw = src_path.read_bytes()[:KEY_FILE_BYTES_LIMIT]
                    text = _decode(raw)
                except Exception:
                    text = ""
                key_files_payload.append({
                    "path": m.path,
                    "size": m.size,
                    "body_preview": text,
                })

            # Build prompt
            user_payload = {
                "container_kind": extracted.container_kind,
                "file_count": len(members),
                "total_uncompressed_bytes": total_bytes,
                "skipped_count": len(extracted.skipped),
                "tree": tree,
                "key_files": key_files_payload,
                "indexed_files_sample": indexed_files,
                "git_metadata": git_meta.to_dict() if git_meta else None,
                "folder_path": ctx.folder_path,
                "sibling_names": ctx.sibling_names,
                "catalog_sketch": ctx.catalog_sketch,
                "tag_vocabulary": ctx.tag_vocabulary,
            }
            user_text = (
                "Index the container described below. Hints are advisory; "
                "the directory tree and key files take precedence.\n\n"
                f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>"
            )

            client = get_chat_client("ingest")
            resp = await client.complete(ChatRequest(
                system=CONTAINER_PIPELINE_SYSTEM,
                messages=[ChatMessage(role="user", content=[
                    TextBlock(text=user_text),
                ])],
                max_tokens=2048,
                json_schema=CONTAINER_PIPELINE_SCHEMA,
                temperature=0.2,
            ))

            if resp.parsed_json is None:
                raise ValueError("container pipeline produced non-JSON output")
            data = resp.parsed_json

            # Handler-bound description: include the structural facts (we
            # don't fully trust the LLM with file_count etc.).
            description = {
                "container_kind": extracted.container_kind,
                "file_count": len(members),
                "total_uncompressed_bytes": total_bytes,
                "primary_language": data["description"].get("primary_language"),
                "frameworks_detected":
                    list(data["description"].get("frameworks_detected") or []),
                "tree": tree,
                "indexed_files": indexed_files,
                "key_files": [
                    {"path": k["path"], "size": k["size"]}
                    for k in key_files_payload
                ],
                "git_metadata": git_meta.to_dict() if git_meta else None,
                "ingest_filters_applied": [
                    reason for _, reason in extracted.skipped[:50]
                ],
            }

            return PipelineResult(
                summary=str(data["summary"]),
                description=description,
                kind="container",
                extra=(data.get("extra") or "") or None,
                entry_extra=(data.get("entry_extra") or "") or None,
                entry_catalog_path=
                    list(data.get("entry_catalog_path") or []) or None,
                entry_tags=[
                    TagSuggestion(name=str(t["name"]), facet=str(t["facet"]))
                    for t in (data.get("entry_tags") or [])
                ],
            )
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    async def _read_bytes(storage: StorageBackend, key: str) -> bytes:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
        return bytes(buf)


def _decode(b: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")
