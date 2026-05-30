"""External retrieval evaluation datasets and metrics.

The first supported source format is BEIR-style local directories:

    corpus.jsonl
    queries.jsonl
    qrels/<split>.tsv  or  qrels.tsv

Import is intentionally synchronous: each corpus document is written as a
normal Marginalia file/entry and immediately passed through ingest_file. When
the import command returns, the dataset is fully indexed and can be evaluated
without a background worker.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.agent.text_query import normalize_text_queries
from marginalia.agent.tools import ToolContext
from marginalia.agent.tools.read_entries_metadata import read_entries_metadata
from marginalia.agent.tools.read_files import read_files
from marginalia.agent.tools.recall_knowledge import (
    load_rerank_documents_by_entry_id,
    recall_knowledge,
    rerank_recall_entries_with_documents,
    score_recall_entries,
    select_evidence_entry_ids,
)
from marginalia.agent.tools.search_metadata import search_metadata
from marginalia.agent.runtime import run_turn
from marginalia.config import get_settings, resolve_profile
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.db.models import AuditEvent, File, FileEntry
from marginalia.db.session import session_scope
from marginalia.llm import ChatMessage, ChatRequest, get_chat_client
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import sessions as sessions_repo
from marginalia.semantic.index import (
    DEFAULT_INDEX_NAME,
    SemanticIndexBuildResult,
    build_semantic_index,
    semantic_entry_rows,
    semantic_recall_configured,
    search_semantic_index_many,
)
from marginalia.semantic.rerank import rerank_configured
from marginalia.services.folders import parse_remote_folder, resolve_or_create_folder
from marginalia.storage import get_storage
from marginalia.tasks.handlers.ingest_file import handle_ingest_file
from marginalia.utils.ids import new_id, storage_prefix


@dataclass(slots=True)
class EvalImportResult:
    name: str
    dataset_dir: Path
    docs_imported: int
    queries: int
    qrels: int
    split: str
    resumed: bool = False
    concurrency: int = 1


@dataclass(slots=True)
class EvalRunResult:
    name: str
    retriever: str
    queries_total: int
    queries_evaluated: int
    queries_skipped: int
    zero_result_rate: float
    no_relevant_at_k_rate: float
    mrr: float
    hit_rate: dict[int, float]
    recall: dict[int, float]
    ndcg: dict[int, float]
    per_query: list[dict[str, Any]]


@dataclass(slots=True)
class EvalAnswerProbeResult:
    name: str
    retriever: str
    query_id: str | None
    query: str
    timed_out: bool
    timeout_seconds: float
    elapsed_ms: int
    retrieval_limit: int
    evidence_limit: int
    relevant_doc_ids: list[str]
    ranked_doc_ids: list[str]
    evidence_doc_ids: list[str]
    cited_entry_ids: list[str]
    cited_doc_ids: list[str]
    expected_labels: list[str]
    predicted_label: str | None
    label_correct: bool | None
    first_relevant_rank: int | None
    evidence_contains_relevant: bool
    answer_cites_relevant: bool
    answer: str
    usage: dict[str, int]
    error: str | None = None


@dataclass(slots=True)
class EvalAnswerRunResult:
    name: str
    retriever: str
    queries_total: int
    queries_evaluated: int
    queries_skipped: int
    timed_out: int
    timeout_seconds: float
    concurrency: int
    total_elapsed_ms: int
    answer_citation_hit_rate: float
    evidence_hit_rate: float
    no_relevant_evidence_rate: float
    avg_first_relevant_rank: float | None
    labels_evaluated: int
    label_accuracy: float | None
    usage: dict[str, int]
    per_query: list[dict[str, Any]]


@dataclass(slots=True)
class EvalReportCompareResult:
    name: str
    queries_total: int
    queries_evaluated: int
    queries_skipped: int
    timed_out: int
    timeout_seconds: float
    concurrency: int
    total_elapsed_ms: int
    rag_wins: int
    react_wins: int
    ties: int
    judge_errors: int
    rag_citation_hit_rate: float
    react_citation_hit_rate: float
    rag_label_accuracy: float | None
    react_label_accuracy: float | None
    avg_react_tool_calls: float | None
    avg_react_llm_calls: float | None
    usage: dict[str, int]
    per_query: list[dict[str, Any]]


@dataclass(slots=True)
class BeirDocument:
    doc_id: str
    title: str
    text: str
    metadata: dict[str, Any]


@dataclass(slots=True)
class BeirQuery:
    query_id: str
    text: str
    metadata: dict[str, Any]


@dataclass(slots=True)
class _ExistingEvalEntry:
    file_id: str
    entry_id: str
    ingested: bool


def eval_root() -> Path:
    return Path(get_settings().marginalia_home).expanduser() / "eval"


async def import_beir_dataset(
    *,
    name: str,
    source_dir: Path,
    split: str = "test",
    limit: int | None = None,
    remote_folder: str | None = None,
    progress_every: int = 25,
    concurrency: int = 1,
    resume: bool = False,
) -> EvalImportResult:
    """Import a local BEIR-style dataset and synchronously ingest documents."""
    _ensure_ingest_profile()
    source_dir = source_dir.expanduser().resolve()
    corpus_path = source_dir / "corpus.jsonl"
    queries_path = source_dir / "queries.jsonl"
    qrels_path = _resolve_qrels_path(source_dir, split=split)
    _require_file(corpus_path)
    _require_file(queries_path)
    _require_file(qrels_path)

    await bootstrap_schema()

    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if manifest_path.exists():
        raise RuntimeError(
            f"eval dataset {name!r} already exists at {dataset_dir}. "
            "Use a fresh name or remove that eval dataset before re-importing."
        )

    if dataset_dir.exists() and not resume:
        raise RuntimeError(
            f"eval dataset directory {dataset_dir} already exists without a "
            "complete manifest. Re-run with --resume to continue a partial "
            "import, or use a fresh name."
        )
    dataset_dir.mkdir(parents=True, exist_ok=resume)
    if not (dataset_dir / "queries.jsonl").exists():
        shutil.copy2(queries_path, dataset_dir / "queries.jsonl")
    if not (dataset_dir / "qrels.tsv").exists():
        shutil.copy2(qrels_path, dataset_dir / "qrels.tsv")

    query_count = sum(1 for _ in iter_beir_queries(queries_path))
    qrel_count = sum(1 for _ in iter_qrels(qrels_path))
    folder_path = remote_folder or f"/eval/{name}/"
    folder_id = await _ensure_folder(folder_path)
    concurrency = max(1, int(concurrency or 1))

    existing = await _load_existing_eval_entries(name)
    doc_map: dict[str, str] = {
        doc_id: entry.entry_id
        for doc_id, entry in existing.items()
        if entry.ingested
    }
    docs = list(iter_beir_corpus(corpus_path, limit=limit))
    pending_docs = [
        doc for doc in docs
        if not existing.get(doc.doc_id, _ExistingEvalEntry("", "", False)).ingested
    ]
    completed = len(doc_map)
    if resume and completed:
        _write_json(dataset_dir / "doc_map.json", doc_map)
        print(f"  resuming with {completed} already ingested document(s)")

    try:
        if concurrency == 1:
            for doc in pending_docs:
                doc_id, entry_id = await _import_one_beir_doc(
                    doc=doc,
                    dataset_name=name,
                    folder_id=folder_id,
                    folder_path=folder_path,
                    existing=existing.get(doc.doc_id),
                )
                doc_map[doc_id] = entry_id
                completed += 1
                if progress_every and completed % progress_every == 0:
                    _write_json(dataset_dir / "doc_map.json", doc_map)
                    print(f"  imported+ingested {completed} document(s)")
        else:
            sem = asyncio.Semaphore(concurrency)

            async def _bounded(doc: BeirDocument) -> tuple[str, str]:
                async with sem:
                    return await _import_one_beir_doc(
                        doc=doc,
                        dataset_name=name,
                        folder_id=folder_id,
                        folder_path=folder_path,
                        existing=existing.get(doc.doc_id),
                    )

            tasks = [asyncio.create_task(_bounded(doc)) for doc in pending_docs]
            try:
                for task in asyncio.as_completed(tasks):
                    doc_id, entry_id = await task
                    doc_map[doc_id] = entry_id
                    completed += 1
                    if progress_every and completed % progress_every == 0:
                        _write_json(dataset_dir / "doc_map.json", doc_map)
                        print(f"  imported+ingested {completed} document(s)")
            except Exception:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

        _write_json(dataset_dir / "doc_map.json", doc_map)
        _write_json(
            manifest_path,
            {
                "name": name,
                "format": "beir",
                "source_dir": str(source_dir),
                "split": split,
                "limit": limit,
                "remote_folder": folder_path,
                "docs_imported": len(doc_map),
                "queries": query_count,
                "qrels": qrel_count,
                "concurrency": concurrency,
                "resumed": resume,
                "created_at": _utcnow().isoformat(),
            },
        )
    except Exception:
        _write_json(
            dataset_dir / "manifest.failed.json",
            {
                "name": name,
                "format": "beir",
                "source_dir": str(source_dir),
                "split": split,
                "docs_imported_before_failure": len(doc_map),
                "concurrency": concurrency,
                "resumed": resume,
                "failed_at": _utcnow().isoformat(),
            },
        )
        raise

    return EvalImportResult(
        name=name,
        dataset_dir=dataset_dir,
        docs_imported=len(doc_map),
        queries=query_count,
        qrels=qrel_count,
        split=split,
        resumed=resume,
        concurrency=concurrency,
    )


async def build_eval_semantic_index(
    *,
    name: str,
    batch_size: int | None = None,
    concurrency: int = 1,
    resume: bool = False,
    progress_every: int = 50,
) -> SemanticIndexBuildResult:
    """Build a local semantic index for an imported eval dataset."""
    await bootstrap_schema()
    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"eval dataset {name!r} is not imported")
    doc_map: dict[str, str] = _read_json(dataset_dir / "doc_map.json")
    entry_ids = list(doc_map.values())
    async with session_scope() as session:
        return await build_semantic_index(
            session,
            index_name=DEFAULT_INDEX_NAME,
            entry_ids=entry_ids,
            batch_size=batch_size,
            concurrency=concurrency,
            resume=resume,
            progress_every=progress_every,
        )


async def run_eval_dataset(
    *,
    name: str,
    retriever: str = "search_metadata",
    k_values: Iterable[int] = (10, 50, 100),
    query_limit: int | None = None,
) -> EvalRunResult:
    """Run retrieval evaluation against an already-imported eval dataset."""
    await bootstrap_schema()
    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"eval dataset {name!r} is not imported")
    doc_map: dict[str, str] = _read_json(dataset_dir / "doc_map.json")
    entry_to_doc = {entry_id: doc_id for doc_id, entry_id in doc_map.items()}
    queries = list(iter_beir_queries(dataset_dir / "queries.jsonl"))
    if query_limit is not None:
        queries = queries[:query_limit]
    qrels = load_qrels(dataset_dir / "qrels.tsv")

    ks = sorted({int(k) for k in k_values if int(k) > 0})
    if not ks:
        ks = [10]
    max_k = max(ks)

    per_query: list[dict[str, Any]] = []
    aggregate = _MetricAccumulator(ks)
    if retriever == "semantic_recall":
        eligible: list[tuple[BeirQuery, dict[str, int]]] = []
        for q in queries:
            relevant = {
                doc_id: rel
                for doc_id, rel in qrels.get(q.query_id, {}).items()
                if doc_id in doc_map and rel > 0
            }
            if not relevant:
                aggregate.skipped += 1
                continue
            eligible.append((q, relevant))
        batched_hits = await search_semantic_index_many(
            [q.text for q, _relevant in eligible],
            limit=max_k,
        )
        for (q, relevant), hits in zip(eligible, batched_hits):
            ranked_docs = [
                entry_to_doc[hit.entry_id]
                for hit in hits
                if hit.entry_id in entry_to_doc
            ]
            scored = _score_query(ranked_docs, relevant, ks)
            aggregate.add(scored, zero_result=not ranked_docs)
            per_query.append({
                "query_id": q.query_id,
                "query": q.text,
                "relevant_doc_ids": sorted(relevant),
                "ranked_doc_ids": ranked_docs,
                **scored,
            })
        return aggregate.result(
            name=name,
            retriever=retriever,
            queries_total=len(queries),
            per_query=per_query,
        )

    async with session_scope() as session:
        if retriever == "recall_knowledge":
            eligible = []
            for q in queries:
                relevant = {
                    doc_id: rel
                    for doc_id, rel in qrels.get(q.query_id, {}).items()
                    if doc_id in doc_map and rel > 0
                }
                if not relevant:
                    aggregate.skipped += 1
                    continue
                eligible.append((q, relevant))
            ranked_many = await _retrieve_entries_many(
                session,
                retriever=retriever,
                queries=[q.text for q, _relevant in eligible],
                limit=max_k,
            )
            for (q, relevant), ranked_entries in zip(eligible, ranked_many):
                ranked_docs = [
                    entry_to_doc[eid]
                    for eid in ranked_entries
                    if eid in entry_to_doc
                ]
                scored = _score_query(ranked_docs, relevant, ks)
                aggregate.add(scored, zero_result=not ranked_docs)
                per_query.append({
                    "query_id": q.query_id,
                    "query": q.text,
                    "relevant_doc_ids": sorted(relevant),
                    "ranked_doc_ids": ranked_docs,
                    **scored,
                })
            return aggregate.result(
                name=name,
                retriever=retriever,
                queries_total=len(queries),
                per_query=per_query,
            )

        for q in queries:
            relevant = {
                doc_id: rel
                for doc_id, rel in qrels.get(q.query_id, {}).items()
                if doc_id in doc_map and rel > 0
            }
            if not relevant:
                aggregate.skipped += 1
                continue
            ranked_entries = await _retrieve_entries(
                session,
                retriever=retriever,
                query=q.text,
                limit=max_k,
            )
            ranked_docs = [
                entry_to_doc[eid]
                for eid in ranked_entries
                if eid in entry_to_doc
            ]
            scored = _score_query(ranked_docs, relevant, ks)
            aggregate.add(scored, zero_result=not ranked_docs)
            per_query.append({
                "query_id": q.query_id,
                "query": q.text,
                "relevant_doc_ids": sorted(relevant),
                "ranked_doc_ids": ranked_docs,
                **scored,
            })

    return aggregate.result(
        name=name,
        retriever=retriever,
        queries_total=len(queries),
        per_query=per_query,
    )


async def run_answer_probe(
    *,
    name: str,
    retriever: str = "recall_knowledge",
    query_id: str | None = None,
    query: str | None = None,
    retrieval_limit: int = 20,
    evidence_limit: int = 10,
    evidence_chars: int = 2_000,
    timeout_seconds: float = 300.0,
    max_tokens: int = 700,
    profile: str = "chat",
) -> EvalAnswerProbeResult:
    """Run a bounded final-answer probe for one eval query.

    This is intentionally not the full interactive agent. It exercises the
    report-critical path directly: retrieve candidates, read bounded source
    text, make one final-answer LLM call, then check whether the answer cited
    an entry mapped to a relevant qrels document.
    """
    _ensure_answer_profile(profile)
    await bootstrap_schema()
    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"eval dataset {name!r} is not imported")

    doc_map: dict[str, str] = _read_json(dataset_dir / "doc_map.json")
    entry_to_doc = {entry_id: doc_id for doc_id, entry_id in doc_map.items()}
    queries = list(iter_beir_queries(dataset_dir / "queries.jsonl"))
    qrels = load_qrels(dataset_dir / "qrels.tsv")
    selected_query_id, selected_query, selected_metadata = _select_answer_query(
        queries=queries,
        qrels=qrels,
        doc_map=doc_map,
        query_id=query_id,
        query=query,
    )
    relevant_doc_ids = sorted(
        doc_id
        for doc_id, rel in qrels.get(selected_query_id or "", {}).items()
        if doc_id in doc_map and rel > 0
    )
    expected_labels = _expected_labels(selected_metadata, relevant_doc_ids)

    timeout = timeout_seconds if timeout_seconds > 0 else None
    started = time.monotonic()
    try:
        return await asyncio.wait_for(
            _run_answer_probe_inner(
                name=name,
                retriever=retriever,
                query_id=selected_query_id,
                query=selected_query,
                retrieval_limit=max(1, retrieval_limit),
                evidence_limit=max(1, evidence_limit),
                evidence_chars=max(500, evidence_chars),
                max_tokens=max(128, max_tokens),
                profile=profile,
                entry_to_doc=entry_to_doc,
                relevant_doc_ids=relevant_doc_ids,
                expected_labels=expected_labels,
                timeout_seconds=timeout_seconds,
                started=started,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return EvalAnswerProbeResult(
            name=name,
            retriever=retriever,
            query_id=selected_query_id,
            query=selected_query,
            timed_out=True,
            timeout_seconds=timeout_seconds,
            elapsed_ms=elapsed_ms,
            retrieval_limit=max(1, retrieval_limit),
            evidence_limit=max(1, evidence_limit),
            relevant_doc_ids=relevant_doc_ids,
            ranked_doc_ids=[],
            evidence_doc_ids=[],
            cited_entry_ids=[],
            cited_doc_ids=[],
            expected_labels=expected_labels,
            predicted_label=None,
            label_correct=False if expected_labels else None,
            first_relevant_rank=None,
            evidence_contains_relevant=False,
            answer_cites_relevant=False,
            answer="",
            usage={},
            error=f"answer probe exceeded {timeout_seconds:.1f}s",
        )


async def run_answer_eval_dataset(
    *,
    name: str,
    retriever: str = "recall_knowledge",
    retrieval_limit: int = 20,
    evidence_limit: int = 10,
    evidence_chars: int = 2_000,
    timeout_seconds: float = 300.0,
    max_tokens: int = 700,
    profile: str = "chat",
    query_limit: int | None = None,
    qrels_only: bool = False,
    concurrency: int = 1,
) -> EvalAnswerRunResult:
    """Run bounded final-answer probes across imported eval queries."""
    _ensure_answer_profile(profile)
    await bootstrap_schema()
    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"eval dataset {name!r} is not imported")

    doc_map: dict[str, str] = _read_json(dataset_dir / "doc_map.json")
    entry_to_doc = {entry_id: doc_id for doc_id, entry_id in doc_map.items()}
    qrels = load_qrels(dataset_dir / "qrels.tsv")
    queries = list(iter_beir_queries(dataset_dir / "queries.jsonl"))
    if qrels_only:
        queries = [
            q for q in queries
            if any(
                rel > 0 and doc_id in doc_map
                for doc_id, rel in qrels.get(q.query_id, {}).items()
            )
        ]
    if query_limit is not None:
        queries = queries[:query_limit]

    timeout = timeout_seconds if timeout_seconds > 0 else None
    concurrency = max(1, int(concurrency or 1))
    semaphore = asyncio.Semaphore(concurrency)
    total_started = time.monotonic()
    work_items: list[tuple[BeirQuery, list[str]]] = []
    skipped = 0
    for q in queries:
        relevant_doc_ids = sorted(
            doc_id
            for doc_id, rel in qrels.get(q.query_id, {}).items()
            if doc_id in doc_map and rel > 0
        )
        if not relevant_doc_ids:
            skipped += 1
            continue
        work_items.append((q, relevant_doc_ids))

    precomputed_ranked: dict[str, list[str]] = {}
    precomputed_evidence: dict[str, list[str]] = {}
    if retriever == "recall_knowledge" and work_items:
        async with session_scope() as session:
            retrieved_many = await _retrieve_entries_many_detail(
                session,
                retriever=retriever,
                queries=[q.text for q, _relevant_doc_ids in work_items],
                limit=max(1, retrieval_limit),
                evidence_limit=max(1, evidence_limit),
            )
        precomputed_ranked = {
            q.query_id: retrieved["ranked_ids"]
            for (q, _relevant_doc_ids), retrieved in zip(work_items, retrieved_many)
        }
        precomputed_evidence = {
            q.query_id: retrieved["evidence_ids"]
            for (q, _relevant_doc_ids), retrieved in zip(work_items, retrieved_many)
        }

    async def _run_one(q: BeirQuery, relevant_doc_ids: list[str]) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            expected_labels = _expected_labels(q.metadata, relevant_doc_ids)
            try:
                probe = await asyncio.wait_for(
                    _run_answer_probe_inner(
                        name=name,
                        retriever=retriever,
                        query_id=q.query_id,
                        query=q.text,
                        retrieval_limit=max(1, retrieval_limit),
                        evidence_limit=max(1, evidence_limit),
                        evidence_chars=max(500, evidence_chars),
                        max_tokens=max(128, max_tokens),
                        profile=profile,
                        entry_to_doc=entry_to_doc,
                        relevant_doc_ids=relevant_doc_ids,
                        expected_labels=expected_labels,
                        timeout_seconds=timeout_seconds,
                        started=started,
                        ranked_entries=precomputed_ranked.get(q.query_id),
                        evidence_entry_ids=precomputed_evidence.get(q.query_id),
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                probe = EvalAnswerProbeResult(
                    name=name,
                    retriever=retriever,
                    query_id=q.query_id,
                    query=q.text,
                    timed_out=True,
                    timeout_seconds=timeout_seconds,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    retrieval_limit=max(1, retrieval_limit),
                    evidence_limit=max(1, evidence_limit),
                    relevant_doc_ids=relevant_doc_ids,
                    ranked_doc_ids=[],
                    evidence_doc_ids=[],
                    cited_entry_ids=[],
                    cited_doc_ids=[],
                    expected_labels=expected_labels,
                    predicted_label=None,
                    label_correct=False if expected_labels else None,
                    first_relevant_rank=None,
                    evidence_contains_relevant=False,
                    answer_cites_relevant=False,
                    answer="",
                    usage={},
                    error=f"answer probe exceeded {timeout_seconds:.1f}s",
                )
            return answer_probe_to_dict(probe)

    per_query = await asyncio.gather(
        *(_run_one(q, relevant_doc_ids) for q, relevant_doc_ids in work_items)
    )

    return _answer_run_result(
        name=name,
        retriever=retriever,
        queries_total=len(queries),
        queries_skipped=skipped,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        total_elapsed_ms=int((time.monotonic() - total_started) * 1000),
        per_query=per_query,
    )


async def run_report_compare_dataset(
    *,
    name: str,
    retriever: str = "recall_knowledge",
    retrieval_limit: int = 20,
    evidence_limit: int = 10,
    evidence_chars: int = 2_000,
    timeout_seconds: float = 300.0,
    max_tokens: int = 900,
    profile: str = "chat",
    judge_profile: str = "chat",
    query_limit: int | None = 30,
    qrels_only: bool = True,
    concurrency: int = 1,
) -> EvalReportCompareResult:
    """Compare one-shot RAG reports with full ReAct reports on the same queries."""
    _ensure_answer_profile(profile)
    _ensure_answer_profile(judge_profile)
    await bootstrap_schema()
    dataset_dir = eval_root() / name
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"eval dataset {name!r} is not imported")

    doc_map: dict[str, str] = _read_json(dataset_dir / "doc_map.json")
    entry_to_doc = {entry_id: doc_id for doc_id, entry_id in doc_map.items()}
    qrels = load_qrels(dataset_dir / "qrels.tsv")
    queries = list(iter_beir_queries(dataset_dir / "queries.jsonl"))
    if qrels_only:
        queries = [
            q for q in queries
            if any(
                rel > 0 and doc_id in doc_map
                for doc_id, rel in qrels.get(q.query_id, {}).items()
            )
        ]
    if query_limit is not None:
        queries = queries[:query_limit]

    work_items: list[tuple[BeirQuery, list[str]]] = []
    skipped = 0
    for q in queries:
        relevant_doc_ids = sorted(
            doc_id
            for doc_id, rel in qrels.get(q.query_id, {}).items()
            if doc_id in doc_map and rel > 0
        )
        if not relevant_doc_ids:
            skipped += 1
            continue
        work_items.append((q, relevant_doc_ids))

    precomputed_ranked: dict[str, list[str]] = {}
    precomputed_evidence: dict[str, list[str]] = {}
    if work_items:
        async with session_scope() as session:
            retrieved_many = await _retrieve_entries_many_detail(
                session,
                retriever=retriever,
                queries=[q.text for q, _relevant_doc_ids in work_items],
                limit=max(1, retrieval_limit),
                evidence_limit=max(1, evidence_limit),
            )
        precomputed_ranked = {
            q.query_id: retrieved["ranked_ids"]
            for (q, _relevant_doc_ids), retrieved in zip(work_items, retrieved_many)
        }
        precomputed_evidence = {
            q.query_id: retrieved["evidence_ids"]
            for (q, _relevant_doc_ids), retrieved in zip(work_items, retrieved_many)
        }

    timeout = timeout_seconds if timeout_seconds > 0 else None
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    total_started = time.monotonic()

    async def _run_one(q: BeirQuery, relevant_doc_ids: list[str]) -> dict[str, Any]:
        async with semaphore:
            started = time.monotonic()
            expected_labels = _expected_labels(q.metadata, relevant_doc_ids)
            ranked_entries = precomputed_ranked.get(q.query_id) or []
            evidence_entry_ids = precomputed_evidence.get(q.query_id) or []
            try:
                rag, react, judge = await asyncio.wait_for(
                    _run_report_compare_one(
                        name=name,
                        retriever=retriever,
                        query_id=q.query_id,
                        query=q.text,
                        retrieval_limit=max(1, retrieval_limit),
                        evidence_limit=max(1, evidence_limit),
                        evidence_chars=max(500, evidence_chars),
                        max_tokens=max(128, max_tokens),
                        profile=profile,
                        judge_profile=judge_profile,
                        entry_to_doc=entry_to_doc,
                        relevant_doc_ids=relevant_doc_ids,
                        expected_labels=expected_labels,
                        timeout_seconds=timeout_seconds,
                        started=started,
                        ranked_entries=ranked_entries,
                        evidence_entry_ids=evidence_entry_ids,
                    ),
                    timeout=timeout,
                )
                timed_out = False
                error = None
            except asyncio.TimeoutError:
                rag = _empty_report_side("rag")
                react = _empty_report_side("react")
                judge = {
                    "winner": "tie",
                    "scores": {},
                    "reason": f"compare-report exceeded {timeout_seconds:.1f}s",
                    "usage": {},
                    "error": "timeout",
                }
                timed_out = True
                error = f"compare-report exceeded {timeout_seconds:.1f}s"
            except Exception as exc:  # noqa: BLE001
                rag = _empty_report_side("rag")
                react = _empty_report_side("react")
                judge = {
                    "winner": "tie",
                    "scores": {},
                    "reason": str(exc)[:300],
                    "usage": {},
                    "error": repr(exc),
                }
                timed_out = False
                error = repr(exc)

            return {
                "query_id": q.query_id,
                "query": q.text,
                "expected_labels": expected_labels,
                "relevant_doc_ids": relevant_doc_ids,
                "timed_out": timed_out,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "error": error,
                "rag": rag,
                "react": react,
                "judge": judge,
            }

    per_query = await asyncio.gather(
        *(_run_one(q, relevant_doc_ids) for q, relevant_doc_ids in work_items)
    )
    return _report_compare_result(
        name=name,
        queries_total=len(queries),
        queries_skipped=skipped,
        timeout_seconds=timeout_seconds,
        concurrency=max(1, int(concurrency or 1)),
        total_elapsed_ms=int((time.monotonic() - total_started) * 1000),
        per_query=per_query,
    )


async def _run_report_compare_one(
    *,
    name: str,
    retriever: str,
    query_id: str,
    query: str,
    retrieval_limit: int,
    evidence_limit: int,
    evidence_chars: int,
    max_tokens: int,
    profile: str,
    judge_profile: str,
    entry_to_doc: Mapping[str, str],
    relevant_doc_ids: list[str],
    expected_labels: list[str],
    timeout_seconds: float,
    started: float,
    ranked_entries: list[str],
    evidence_entry_ids: list[str],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rag = await _run_rag_report_probe(
        name=name,
        retriever=retriever,
        query_id=query_id,
        query=query,
        retrieval_limit=retrieval_limit,
        evidence_limit=evidence_limit,
        evidence_chars=evidence_chars,
        max_tokens=max_tokens,
        profile=profile,
        entry_to_doc=entry_to_doc,
        relevant_doc_ids=relevant_doc_ids,
        expected_labels=expected_labels,
        timeout_seconds=timeout_seconds,
        started=started,
        ranked_entries=ranked_entries,
        evidence_entry_ids=evidence_entry_ids,
    )
    react = await _run_react_report_probe(
        name=name,
        query_id=query_id,
        query=query,
        profile=profile,
        entry_to_doc=entry_to_doc,
        relevant_doc_ids=relevant_doc_ids,
        expected_labels=expected_labels,
        timeout_seconds=timeout_seconds,
    )
    judge = await _judge_report_pair(
        query=query,
        rag_answer=rag["answer"],
        react_answer=react["answer"],
        expected_labels=expected_labels,
        profile=judge_profile,
    )
    return rag, react, judge


def iter_beir_corpus(path: Path, *, limit: int | None = None) -> Iterable[BeirDocument]:
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            doc_id = str(obj.get("_id") or obj.get("id") or "").strip()
            if not doc_id:
                continue
            yield BeirDocument(
                doc_id=doc_id,
                title=str(obj.get("title") or ""),
                text=str(obj.get("text") or ""),
                metadata=obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {},
            )
            count += 1
            if limit is not None and count >= limit:
                break


def iter_beir_queries(path: Path) -> Iterable[BeirQuery]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            query_id = str(obj.get("_id") or obj.get("id") or "").strip()
            text = str(obj.get("text") or "").strip()
            if query_id and text:
                metadata = obj.get("metadata")
                yield BeirQuery(
                    query_id=query_id,
                    text=text,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )


def iter_qrels(path: Path) -> Iterable[tuple[str, str, int]]:
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            if not parts:
                continue
            head = [p.lower() for p in parts[:3]]
            if head[:2] in (["query-id", "corpus-id"], ["query_id", "corpus_id"]):
                continue
            if len(parts) >= 3:
                if len(parts) >= 4 and parts[1] in {"0", "Q0", "q0"}:
                    qid, doc_id, rel_raw = parts[0], parts[2], parts[3]
                else:
                    qid, doc_id, rel_raw = parts[0], parts[1], parts[2]
                try:
                    relevance = int(float(rel_raw))
                except ValueError:
                    continue
                yield qid, doc_id, relevance


def load_qrels(path: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for qid, doc_id, relevance in iter_qrels(path):
        out.setdefault(qid, {})[doc_id] = relevance
    return out


def result_to_dict(result: EvalRunResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "retriever": result.retriever,
        "queries_total": result.queries_total,
        "queries_evaluated": result.queries_evaluated,
        "queries_skipped": result.queries_skipped,
        "zero_result_rate": result.zero_result_rate,
        "no_relevant_at_k_rate": result.no_relevant_at_k_rate,
        "mrr": result.mrr,
        "hit_rate": {str(k): v for k, v in result.hit_rate.items()},
        "recall": {str(k): v for k, v in result.recall.items()},
        "ndcg": {str(k): v for k, v in result.ndcg.items()},
        "per_query": result.per_query,
    }


def answer_probe_to_dict(result: EvalAnswerProbeResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "retriever": result.retriever,
        "query_id": result.query_id,
        "query": result.query,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "elapsed_ms": result.elapsed_ms,
        "retrieval_limit": result.retrieval_limit,
        "evidence_limit": result.evidence_limit,
        "relevant_doc_ids": result.relevant_doc_ids,
        "ranked_doc_ids": result.ranked_doc_ids,
        "evidence_doc_ids": result.evidence_doc_ids,
        "cited_entry_ids": result.cited_entry_ids,
        "cited_doc_ids": result.cited_doc_ids,
        "expected_labels": result.expected_labels,
        "predicted_label": result.predicted_label,
        "label_correct": result.label_correct,
        "first_relevant_rank": result.first_relevant_rank,
        "evidence_contains_relevant": result.evidence_contains_relevant,
        "answer_cites_relevant": result.answer_cites_relevant,
        "answer": result.answer,
        "usage": result.usage,
        "error": result.error,
    }


def answer_run_to_dict(result: EvalAnswerRunResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "retriever": result.retriever,
        "queries_total": result.queries_total,
        "queries_evaluated": result.queries_evaluated,
        "queries_skipped": result.queries_skipped,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "concurrency": result.concurrency,
        "total_elapsed_ms": result.total_elapsed_ms,
        "answer_citation_hit_rate": result.answer_citation_hit_rate,
        "evidence_hit_rate": result.evidence_hit_rate,
        "no_relevant_evidence_rate": result.no_relevant_evidence_rate,
        "avg_first_relevant_rank": result.avg_first_relevant_rank,
        "labels_evaluated": result.labels_evaluated,
        "label_accuracy": result.label_accuracy,
        "usage": result.usage,
        "per_query": result.per_query,
    }


def report_compare_to_dict(result: EvalReportCompareResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "queries_total": result.queries_total,
        "queries_evaluated": result.queries_evaluated,
        "queries_skipped": result.queries_skipped,
        "timed_out": result.timed_out,
        "timeout_seconds": result.timeout_seconds,
        "concurrency": result.concurrency,
        "total_elapsed_ms": result.total_elapsed_ms,
        "rag_wins": result.rag_wins,
        "react_wins": result.react_wins,
        "ties": result.ties,
        "judge_errors": result.judge_errors,
        "rag_citation_hit_rate": result.rag_citation_hit_rate,
        "react_citation_hit_rate": result.react_citation_hit_rate,
        "rag_label_accuracy": result.rag_label_accuracy,
        "react_label_accuracy": result.react_label_accuracy,
        "avg_react_tool_calls": result.avg_react_tool_calls,
        "avg_react_llm_calls": result.avg_react_llm_calls,
        "usage": result.usage,
        "per_query": result.per_query,
    }


def format_run_result(result: EvalRunResult) -> str:
    lines = [
        f"dataset: {result.name}",
        f"retriever: {result.retriever}",
        (
            f"queries: {result.queries_evaluated} evaluated / "
            f"{result.queries_total} total"
        ),
        f"skipped_no_imported_relevance: {result.queries_skipped}",
        f"zero_result_rate: {result.zero_result_rate:.4f}",
        f"no_relevant@max_k_rate: {result.no_relevant_at_k_rate:.4f}",
        f"MRR: {result.mrr:.4f}",
        "",
        "k\thit\tcandidate_recall\tndcg",
    ]
    for k in sorted(result.recall):
        lines.append(
            f"{k}\t{result.hit_rate[k]:.4f}\t"
            f"{result.recall[k]:.4f}\t{result.ndcg[k]:.4f}"
        )
    return "\n".join(lines)


def format_answer_run_result(result: EvalAnswerRunResult) -> str:
    lines = [
        f"dataset: {result.name}",
        f"retriever: {result.retriever}",
        (
            f"queries: {result.queries_evaluated} evaluated / "
            f"{result.queries_total} total"
        ),
        f"skipped_no_imported_relevance: {result.queries_skipped}",
        f"timed_out: {result.timed_out}",
        f"timeout_seconds_per_query: {result.timeout_seconds:.1f}",
        f"concurrency: {result.concurrency}",
        f"total_elapsed_ms: {result.total_elapsed_ms}",
        f"evidence_hit_rate: {result.evidence_hit_rate:.4f}",
        f"no_relevant_evidence_rate: {result.no_relevant_evidence_rate:.4f}",
        f"answer_citation_hit_rate: {result.answer_citation_hit_rate:.4f}",
    ]
    if result.avg_first_relevant_rank is not None:
        lines.append(f"avg_first_relevant_rank: {result.avg_first_relevant_rank:.4f}")
    else:
        lines.append("avg_first_relevant_rank: (none)")
    if result.label_accuracy is not None:
        lines.append(
            f"label_accuracy: {result.label_accuracy:.4f} "
            f"({result.labels_evaluated} labeled)"
        )
    else:
        lines.append("label_accuracy: (no labels)")
    return "\n".join(lines)


def format_report_compare_result(result: EvalReportCompareResult) -> str:
    lines = [
        f"dataset: {result.name}",
        (
            f"queries: {result.queries_evaluated} evaluated / "
            f"{result.queries_total} total"
        ),
        f"skipped_no_imported_relevance: {result.queries_skipped}",
        f"timed_out: {result.timed_out}",
        f"timeout_seconds_per_query: {result.timeout_seconds:.1f}",
        f"concurrency: {result.concurrency}",
        f"total_elapsed_ms: {result.total_elapsed_ms}",
        (
            "judge_wins: "
            f"rag={result.rag_wins} react={result.react_wins} ties={result.ties}"
        ),
        f"judge_errors: {result.judge_errors}",
        f"rag_citation_hit_rate: {result.rag_citation_hit_rate:.4f}",
        f"react_citation_hit_rate: {result.react_citation_hit_rate:.4f}",
    ]
    if result.rag_label_accuracy is not None:
        lines.append(f"rag_label_accuracy: {result.rag_label_accuracy:.4f}")
    else:
        lines.append("rag_label_accuracy: (no labels)")
    if result.react_label_accuracy is not None:
        lines.append(f"react_label_accuracy: {result.react_label_accuracy:.4f}")
    else:
        lines.append("react_label_accuracy: (no labels)")
    if result.avg_react_tool_calls is not None:
        lines.append(f"avg_react_tool_calls: {result.avg_react_tool_calls:.4f}")
    else:
        lines.append("avg_react_tool_calls: (none)")
    if result.avg_react_llm_calls is not None:
        lines.append(f"avg_react_llm_calls: {result.avg_react_llm_calls:.4f}")
    else:
        lines.append("avg_react_llm_calls: (none)")
    return "\n".join(lines)


def format_answer_probe_result(result: EvalAnswerProbeResult) -> str:
    lines = [
        f"dataset: {result.name}",
        f"retriever: {result.retriever}",
        f"query_id: {result.query_id or '(ad hoc)'}",
        f"elapsed_ms: {result.elapsed_ms}",
        f"timed_out: {str(result.timed_out).lower()}",
        f"retrieval_limit: {result.retrieval_limit}",
        f"evidence_limit: {result.evidence_limit}",
        f"first_relevant_rank: {result.first_relevant_rank}",
        f"evidence_contains_relevant: {str(result.evidence_contains_relevant).lower()}",
        f"answer_cites_relevant: {str(result.answer_cites_relevant).lower()}",
        f"expected_labels: {', '.join(result.expected_labels) or '(none)'}",
        f"predicted_label: {result.predicted_label or '(none)'}",
        f"label_correct: {_format_optional_bool(result.label_correct)}",
        f"relevant_doc_ids: {', '.join(result.relevant_doc_ids) or '(none)'}",
        f"evidence_doc_ids: {', '.join(result.evidence_doc_ids) or '(none)'}",
        f"cited_doc_ids: {', '.join(result.cited_doc_ids) or '(none)'}",
    ]
    if result.error:
        lines.append(f"error: {result.error}")
    lines.extend(["", "answer:", result.answer or "(no answer)"])
    return "\n".join(lines)


def _answer_run_result(
    *,
    name: str,
    retriever: str,
    queries_total: int,
    queries_skipped: int,
    timeout_seconds: float,
    concurrency: int,
    total_elapsed_ms: int,
    per_query: list[dict[str, Any]],
) -> EvalAnswerRunResult:
    evaluated = len(per_query)
    denom = max(1, evaluated)
    timed_out = sum(1 for row in per_query if row.get("timed_out"))
    evidence_hits = sum(1 for row in per_query if row.get("evidence_contains_relevant"))
    citation_hits = sum(1 for row in per_query if row.get("answer_cites_relevant"))
    label_rows = [row for row in per_query if row.get("label_correct") is not None]
    label_hits = sum(1 for row in label_rows if row.get("label_correct"))
    ranks = [
        int(row["first_relevant_rank"])
        for row in per_query
        if row.get("first_relevant_rank") is not None
    ]
    usage: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    for row in per_query:
        row_usage = row.get("usage") or {}
        if not isinstance(row_usage, Mapping):
            continue
        for key in usage:
            usage[key] += int(row_usage.get(key) or 0)
    return EvalAnswerRunResult(
        name=name,
        retriever=retriever,
        queries_total=queries_total,
        queries_evaluated=evaluated,
        queries_skipped=queries_skipped,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        total_elapsed_ms=total_elapsed_ms,
        answer_citation_hit_rate=citation_hits / denom,
        evidence_hit_rate=evidence_hits / denom,
        no_relevant_evidence_rate=(evaluated - evidence_hits) / denom,
        avg_first_relevant_rank=(sum(ranks) / len(ranks)) if ranks else None,
        labels_evaluated=len(label_rows),
        label_accuracy=(label_hits / len(label_rows)) if label_rows else None,
        usage=usage,
        per_query=per_query,
    )


def _report_compare_result(
    *,
    name: str,
    queries_total: int,
    queries_skipped: int,
    timeout_seconds: float,
    concurrency: int,
    total_elapsed_ms: int,
    per_query: list[dict[str, Any]],
) -> EvalReportCompareResult:
    evaluated = len(per_query)
    denom = max(1, evaluated)
    timed_out = sum(1 for row in per_query if row.get("timed_out"))
    rag_wins = 0
    react_wins = 0
    ties = 0
    judge_errors = 0
    rag_citation_hits = 0
    react_citation_hits = 0
    rag_label_rows: list[dict[str, Any]] = []
    react_label_rows: list[dict[str, Any]] = []
    react_tool_calls: list[int] = []
    react_llm_calls: list[int] = []
    usage = _empty_compare_usage()

    for row in per_query:
        judge = row.get("judge") if isinstance(row.get("judge"), Mapping) else {}
        winner = str((judge or {}).get("winner") or "tie").lower()
        if winner == "rag":
            rag_wins += 1
        elif winner == "react":
            react_wins += 1
        else:
            ties += 1
        if (judge or {}).get("error"):
            judge_errors += 1

        rag = row.get("rag") if isinstance(row.get("rag"), Mapping) else {}
        react = row.get("react") if isinstance(row.get("react"), Mapping) else {}
        if (rag or {}).get("answer_cites_relevant"):
            rag_citation_hits += 1
        if (react or {}).get("answer_cites_relevant"):
            react_citation_hits += 1
        if (rag or {}).get("label_correct") is not None:
            rag_label_rows.append(dict(rag or {}))
        if (react or {}).get("label_correct") is not None:
            react_label_rows.append(dict(react or {}))
        if (react or {}).get("tool_calls") is not None:
            react_tool_calls.append(int((react or {}).get("tool_calls") or 0))
        if (react or {}).get("llm_calls") is not None:
            react_llm_calls.append(int((react or {}).get("llm_calls") or 0))

        _accumulate_compare_usage(usage, "rag", (rag or {}).get("usage"))
        _accumulate_compare_usage(usage, "react", (react or {}).get("usage"))
        _accumulate_compare_usage(usage, "judge", (judge or {}).get("usage"))

    rag_label_hits = sum(1 for row in rag_label_rows if row.get("label_correct"))
    react_label_hits = sum(1 for row in react_label_rows if row.get("label_correct"))
    return EvalReportCompareResult(
        name=name,
        queries_total=queries_total,
        queries_evaluated=evaluated,
        queries_skipped=queries_skipped,
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        total_elapsed_ms=total_elapsed_ms,
        rag_wins=rag_wins,
        react_wins=react_wins,
        ties=ties,
        judge_errors=judge_errors,
        rag_citation_hit_rate=rag_citation_hits / denom,
        react_citation_hit_rate=react_citation_hits / denom,
        rag_label_accuracy=(
            rag_label_hits / len(rag_label_rows) if rag_label_rows else None
        ),
        react_label_accuracy=(
            react_label_hits / len(react_label_rows) if react_label_rows else None
        ),
        avg_react_tool_calls=(
            sum(react_tool_calls) / len(react_tool_calls) if react_tool_calls else None
        ),
        avg_react_llm_calls=(
            sum(react_llm_calls) / len(react_llm_calls) if react_llm_calls else None
        ),
        usage=usage,
        per_query=per_query,
    )


def _empty_compare_usage() -> dict[str, int]:
    usage: dict[str, int] = {}
    for prefix in ("rag", "react", "judge", "total"):
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        ):
            usage[f"{prefix}_{key}"] = 0
    return usage


def _accumulate_compare_usage(
    usage: dict[str, int],
    prefix: str,
    row_usage: Any,
) -> None:
    if not isinstance(row_usage, Mapping):
        return
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    ):
        value = int(row_usage.get(key) or 0)
        usage[f"{prefix}_{key}"] += value
        usage[f"total_{key}"] += value


def _format_optional_bool(value: bool | None) -> str:
    if value is None:
        return "(none)"
    return str(value).lower()


def _ensure_ingest_profile() -> None:
    profile = resolve_profile(get_settings(), "ingest")
    if not profile.api_key:
        raise RuntimeError(
            "LLM ingest profile is not configured. Set LLM_DEFAULT_API_KEY "
            "or LLM_INGEST_API_KEY before importing an eval dataset."
        )


def _ensure_answer_profile(profile_name: str) -> None:
    profile = resolve_profile(get_settings(), profile_name)
    if not profile.api_key:
        env_name = f"LLM_{profile_name.upper()}_API_KEY"
        raise RuntimeError(
            f"LLM {profile_name!r} profile is not configured. Set "
            f"LLM_DEFAULT_API_KEY or {env_name} before running answer eval."
        )


async def _ensure_folder(remote_folder: str) -> str | None:
    segments = parse_remote_folder(remote_folder)
    async with session_scope() as session:
        folder = await resolve_or_create_folder(session, segments)
        await session.commit()
        return folder.id if folder is not None else None


async def _load_existing_eval_entries(
    dataset_name: str,
) -> dict[str, _ExistingEvalEntry]:
    """Recover doc_id -> file/entry mapping from audit events for resume."""
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(AuditEvent.kind, AuditEvent.payload)
                .where(AuditEvent.kind.in_(("file_created", "entry_created")))
            )
        ).all()

        file_by_doc: dict[str, str] = {}
        entry_by_doc: dict[str, str] = {}
        for kind, payload in rows:
            if not isinstance(payload, Mapping):
                continue
            if payload.get("source") != "eval_import":
                continue
            if payload.get("dataset") != dataset_name:
                continue
            doc_id = str(payload.get("doc_id") or "")
            if not doc_id:
                continue
            if kind == "file_created" and payload.get("file_id"):
                file_by_doc[doc_id] = str(payload["file_id"])
            elif kind == "entry_created" and payload.get("entry_id"):
                entry_by_doc[doc_id] = str(payload["entry_id"])

        file_ids = list(file_by_doc.values())
        ingested_by_file: dict[str, bool] = {}
        if file_ids:
            file_rows = (
                await session.execute(
                    select(File.id, File.ingested_at).where(File.id.in_(file_ids))
                )
            ).all()
            ingested_by_file = {
                file_id: ingested_at is not None
                for file_id, ingested_at in file_rows
            }
        await session.commit()

    out: dict[str, _ExistingEvalEntry] = {}
    for doc_id, file_id in file_by_doc.items():
        entry_id = entry_by_doc.get(doc_id)
        if not entry_id:
            continue
        out[doc_id] = _ExistingEvalEntry(
            file_id=file_id,
            entry_id=entry_id,
            ingested=bool(ingested_by_file.get(file_id)),
        )
    return out


async def _import_one_beir_doc(
    *,
    doc: BeirDocument,
    dataset_name: str,
    folder_id: str | None,
    folder_path: str,
    existing: _ExistingEvalEntry | None,
) -> tuple[str, str]:
    if existing is not None:
        await _ingest_eval_file_with_retries(existing.file_id)
        return doc.doc_id, existing.entry_id

    file_id, entry_id = await _create_eval_text_entry(
        doc=doc,
        dataset_name=dataset_name,
        folder_id=folder_id,
        folder_path=folder_path,
    )
    await _ingest_eval_file_with_retries(file_id)
    return doc.doc_id, entry_id


async def _ingest_eval_file_with_retries(file_id: str, *, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await handle_ingest_file({"file_id": file_id})
            return
        except Exception:
            if attempt >= attempts:
                raise
            await asyncio.sleep(min(10.0, 0.5 * (2 ** (attempt - 1))))


async def _create_eval_text_entry(
    *,
    doc: BeirDocument,
    dataset_name: str,
    folder_id: str | None,
    folder_path: str,
) -> tuple[str, str]:
    now = _utcnow()
    file_id = new_id()
    entry_id = new_id()
    display_name = _doc_display_name(doc.doc_id)
    body = _render_document(doc)
    data = body.encode("utf-8")
    sha256 = hashlib.sha256(data).hexdigest()
    top, sub = storage_prefix(file_id)
    suggested_key = f"{top}/{sub}/{file_id}"
    storage_key = await get_storage().put(
        suggested_key,
        _one_chunk(data),
        size=len(data),
        content_type="text/plain",
        display_name=display_name,
        folder_path=folder_path,
    )

    async with session_scope() as session:
        session.add(File(
            id=file_id,
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=len(data),
            mime_type="text/plain",
            original_ext=".txt",
            kind=None,
            summary=None,
            description=None,
            extra=None,
            ingest_status="pending",
            ingested_at=None,
            deleted_at=None,
            created_at=now,
            updated_at=now,
        ))
        session.add(FileEntry(
            id=entry_id,
            folder_id=folder_id,
            file_id=file_id,
            display_name=display_name,
            lifecycle="active",
            catalog_id=None,
            extra=None,
            deleted_at=None,
            purge_after=None,
            created_at=now,
            updated_at=now,
        ))
        await audit_events_repo.append(
            session,
            kind="file_created",
            payload={
                "file_id": file_id,
                "sha256": sha256,
                "size_bytes": len(data),
                "mime_type": "text/plain",
                "source": "eval_import",
                "dataset": dataset_name,
                "doc_id": doc.doc_id,
            },
        )
        await audit_events_repo.append(
            session,
            kind="entry_created",
            payload={
                "entry_id": entry_id,
                "folder_id": folder_id,
                "file_id": file_id,
                "display_name": display_name,
                "deduped": False,
                "source": "eval_import",
                "dataset": dataset_name,
                "doc_id": doc.doc_id,
            },
        )
        await session.commit()
    return file_id, entry_id


def _select_answer_query(
    *,
    queries: list[BeirQuery],
    qrels: Mapping[str, Mapping[str, int]],
    doc_map: Mapping[str, str],
    query_id: str | None,
    query: str | None,
) -> tuple[str | None, str, dict[str, Any]]:
    if query is not None and query.strip():
        metadata = {}
        if query_id:
            metadata = next(
                (q.metadata for q in queries if q.query_id == query_id),
                {},
            )
        return query_id, query.strip(), metadata

    by_id = {q.query_id: q for q in queries}
    if query_id:
        if query_id not in by_id:
            raise RuntimeError(f"query_id {query_id!r} not found in eval dataset")
        selected = by_id[query_id]
        return query_id, selected.text, selected.metadata

    for q in queries:
        relevant = [
            doc_id
            for doc_id, rel in qrels.get(q.query_id, {}).items()
            if rel > 0 and doc_id in doc_map
        ]
        if relevant:
            return q.query_id, q.text, q.metadata
    if queries:
        return queries[0].query_id, queries[0].text, queries[0].metadata
    raise RuntimeError("eval dataset has no queries")


async def _run_answer_probe_inner(
    *,
    name: str,
    retriever: str,
    query_id: str | None,
    query: str,
    retrieval_limit: int,
    evidence_limit: int,
    evidence_chars: int,
    max_tokens: int,
    profile: str,
    entry_to_doc: Mapping[str, str],
    relevant_doc_ids: list[str],
    expected_labels: list[str],
    timeout_seconds: float,
    started: float,
    ranked_entries: list[str] | None = None,
    evidence_entry_ids: list[str] | None = None,
) -> EvalAnswerProbeResult:
    async with session_scope() as session:
        if ranked_entries is None:
            ranked_entries = await _retrieve_entries(
                session,
                retriever=retriever,
                query=query,
                limit=retrieval_limit,
            )
        if evidence_entry_ids is None:
            evidence_entry_ids = [
                eid for eid in ranked_entries if eid in entry_to_doc
            ][:evidence_limit]
        else:
            evidence_entry_ids = [
                eid for eid in evidence_entry_ids if eid in entry_to_doc
            ][:evidence_limit]
        evidence = await _read_answer_evidence(
            session,
            query=query,
            entry_ids=evidence_entry_ids,
            entry_to_doc=entry_to_doc,
            evidence_chars=evidence_chars,
        )

    ranked_doc_ids = [
        entry_to_doc[eid]
        for eid in ranked_entries
        if eid in entry_to_doc
    ]
    evidence_doc_ids = [
        entry_to_doc[eid]
        for eid in evidence_entry_ids
        if eid in entry_to_doc
    ]
    rel_set = set(relevant_doc_ids)
    first_relevant_rank = next(
        (idx + 1 for idx, doc_id in enumerate(ranked_doc_ids) if doc_id in rel_set),
        None,
    )
    answer, usage = await _complete_answer_probe(
        query=query,
        evidence=evidence,
        profile=profile,
        max_tokens=max_tokens,
    )
    cited_entry_ids = _extract_cited_entry_ids(
        answer,
        known_entry_ids=entry_to_doc.keys(),
    )
    cited_doc_ids = [
        entry_to_doc[eid]
        for eid in cited_entry_ids
        if eid in entry_to_doc
    ]
    predicted_label = _predict_answer_label(answer)
    label_correct = (
        predicted_label in set(expected_labels)
        if expected_labels and predicted_label is not None
        else (False if expected_labels else None)
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return EvalAnswerProbeResult(
        name=name,
        retriever=retriever,
        query_id=query_id,
        query=query,
        timed_out=False,
        timeout_seconds=timeout_seconds,
        elapsed_ms=elapsed_ms,
        retrieval_limit=retrieval_limit,
        evidence_limit=evidence_limit,
        relevant_doc_ids=relevant_doc_ids,
        ranked_doc_ids=ranked_doc_ids,
        evidence_doc_ids=evidence_doc_ids,
        cited_entry_ids=cited_entry_ids,
        cited_doc_ids=cited_doc_ids,
        expected_labels=expected_labels,
        predicted_label=predicted_label,
        label_correct=label_correct,
        first_relevant_rank=first_relevant_rank,
        evidence_contains_relevant=bool(set(evidence_doc_ids).intersection(rel_set)),
        answer_cites_relevant=bool(set(cited_doc_ids).intersection(rel_set)),
        answer=answer,
        usage=usage,
    )


async def _run_rag_report_probe(
    *,
    name: str,
    retriever: str,
    query_id: str,
    query: str,
    retrieval_limit: int,
    evidence_limit: int,
    evidence_chars: int,
    max_tokens: int,
    profile: str,
    entry_to_doc: Mapping[str, str],
    relevant_doc_ids: list[str],
    expected_labels: list[str],
    timeout_seconds: float,
    started: float,
    ranked_entries: list[str],
    evidence_entry_ids: list[str],
) -> dict[str, Any]:
    side_started = time.monotonic()
    async with session_scope() as session:
        evidence_entry_ids = [
            eid for eid in evidence_entry_ids if eid in entry_to_doc
        ][:evidence_limit]
        evidence = await _read_answer_evidence(
            session,
            query=query,
            entry_ids=evidence_entry_ids,
            entry_to_doc=entry_to_doc,
            evidence_chars=evidence_chars,
        )

    ranked_doc_ids = [
        entry_to_doc[eid]
        for eid in ranked_entries
        if eid in entry_to_doc
    ]
    evidence_doc_ids = [
        entry_to_doc[eid]
        for eid in evidence_entry_ids
        if eid in entry_to_doc
    ]
    rel_set = set(relevant_doc_ids)
    first_relevant_rank = next(
        (idx + 1 for idx, doc_id in enumerate(ranked_doc_ids) if doc_id in rel_set),
        None,
    )
    answer, usage = await _complete_report_probe(
        query=query,
        evidence=evidence,
        profile=profile,
        max_tokens=max_tokens,
    )
    return _report_side_result(
        kind="rag",
        name=name,
        retriever=retriever,
        query_id=query_id,
        query=query,
        timeout_seconds=timeout_seconds,
        elapsed_ms=int((time.monotonic() - side_started) * 1000),
        total_elapsed_ms=int((time.monotonic() - started) * 1000),
        retrieval_limit=retrieval_limit,
        evidence_limit=evidence_limit,
        relevant_doc_ids=relevant_doc_ids,
        ranked_doc_ids=ranked_doc_ids,
        evidence_doc_ids=evidence_doc_ids,
        answer=answer,
        usage=usage,
        entry_to_doc=entry_to_doc,
        expected_labels=expected_labels,
        first_relevant_rank=first_relevant_rank,
    )


async def _run_react_report_probe(
    *,
    name: str,
    query_id: str,
    query: str,
    profile: str,
    entry_to_doc: Mapping[str, str],
    relevant_doc_ids: list[str],
    expected_labels: list[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    side_started = time.monotonic()
    session_id = ""
    conversation_id: str | None = None
    answer = ""
    answer_event = ""
    done_payload: dict[str, Any] = {}
    tool_names: list[str] = []

    async with session_scope() as session:
        row = await sessions_repo.create_session(
            session,
            initiating_user_message=_truncate(query, 160),
        )
        await session.commit()
        session_id = row.id

    try:
        async for event in run_turn(
            session_id=session_id,
            user_message=_render_react_report_user_prompt(query),
        ):
            if event.event_type == "conversation":
                conversation_id = event.data
            elif event.event_type == "answer":
                answer_event = event.data or answer_event
            elif event.event_type == "tool_call":
                payload = _parse_json_object(event.data)
                name_value = payload.get("name") if isinstance(payload, Mapping) else None
                if name_value:
                    tool_names.append(str(name_value))
            elif event.event_type == "done":
                payload = _parse_json_object(event.data)
                if isinstance(payload, dict):
                    done_payload = payload
    finally:
        async with session_scope() as session:
            if conversation_id:
                conv = await sessions_repo.get_conversation(session, conversation_id)
                if conv is not None and conv.agent_response:
                    answer = conv.agent_response
            if session_id:
                await sessions_repo.close_session(
                    session,
                    session_id=session_id,
                    end_reason="normal",
                )
            await session.commit()

    if not answer:
        answer = answer_event
    usage = {
        "input_tokens": int(done_payload.get("tokens_in") or 0),
        "output_tokens": int(done_payload.get("tokens_out") or 0),
        "cache_read_tokens": int(done_payload.get("cache_read") or 0),
        "cache_creation_tokens": int(done_payload.get("cache_creation_tokens") or 0),
    }
    side = _report_side_result(
        kind="react",
        name=name,
        retriever="react",
        query_id=query_id,
        query=query,
        timeout_seconds=timeout_seconds,
        elapsed_ms=int((time.monotonic() - side_started) * 1000),
        total_elapsed_ms=int((time.monotonic() - side_started) * 1000),
        retrieval_limit=0,
        evidence_limit=0,
        relevant_doc_ids=relevant_doc_ids,
        ranked_doc_ids=[],
        evidence_doc_ids=[],
        answer=answer,
        usage=usage,
        entry_to_doc=entry_to_doc,
        expected_labels=expected_labels,
        first_relevant_rank=None,
    )
    side.update({
        "session_id": session_id,
        "conversation_id": conversation_id,
        "tool_names": tool_names,
        "tool_calls": int(done_payload.get("tool_calls") or len(tool_names)),
        "llm_calls": int(done_payload.get("llm_calls") or 0),
        "duration_ms": int(done_payload.get("duration_ms") or side["elapsed_ms"]),
        "truncated": bool(done_payload.get("truncated")),
        "runtime_profile": "chat",
        "requested_profile": profile,
    })
    return side


def _report_side_result(
    *,
    kind: str,
    name: str,
    retriever: str,
    query_id: str,
    query: str,
    timeout_seconds: float,
    elapsed_ms: int,
    total_elapsed_ms: int,
    retrieval_limit: int,
    evidence_limit: int,
    relevant_doc_ids: list[str],
    ranked_doc_ids: list[str],
    evidence_doc_ids: list[str],
    answer: str,
    usage: dict[str, int],
    entry_to_doc: Mapping[str, str],
    expected_labels: list[str],
    first_relevant_rank: int | None,
) -> dict[str, Any]:
    rel_set = set(relevant_doc_ids)
    cited_entry_ids = _extract_cited_entry_ids(
        answer,
        known_entry_ids=entry_to_doc.keys(),
    )
    cited_doc_ids = [
        entry_to_doc[eid]
        for eid in cited_entry_ids
        if eid in entry_to_doc
    ]
    predicted_label = _predict_answer_label(answer)
    label_correct = (
        predicted_label in set(expected_labels)
        if expected_labels and predicted_label is not None
        else (False if expected_labels else None)
    )
    return {
        "kind": kind,
        "name": name,
        "retriever": retriever,
        "query_id": query_id,
        "query": query,
        "timed_out": False,
        "timeout_seconds": timeout_seconds,
        "elapsed_ms": elapsed_ms,
        "total_elapsed_ms": total_elapsed_ms,
        "retrieval_limit": retrieval_limit,
        "evidence_limit": evidence_limit,
        "relevant_doc_ids": relevant_doc_ids,
        "ranked_doc_ids": ranked_doc_ids,
        "evidence_doc_ids": evidence_doc_ids,
        "cited_entry_ids": cited_entry_ids,
        "cited_doc_ids": cited_doc_ids,
        "expected_labels": expected_labels,
        "predicted_label": predicted_label,
        "label_correct": label_correct,
        "first_relevant_rank": first_relevant_rank,
        "evidence_contains_relevant": bool(set(evidence_doc_ids).intersection(rel_set)),
        "answer_cites_relevant": bool(set(cited_doc_ids).intersection(rel_set)),
        "answer": answer,
        "usage": usage,
        "error": None,
    }


def _empty_report_side(kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "timed_out": True,
        "timeout_seconds": 0.0,
        "elapsed_ms": 0,
        "total_elapsed_ms": 0,
        "retrieval_limit": 0,
        "evidence_limit": 0,
        "relevant_doc_ids": [],
        "ranked_doc_ids": [],
        "evidence_doc_ids": [],
        "cited_entry_ids": [],
        "cited_doc_ids": [],
        "expected_labels": [],
        "predicted_label": None,
        "label_correct": None,
        "first_relevant_rank": None,
        "evidence_contains_relevant": False,
        "answer_cites_relevant": False,
        "answer": "",
        "usage": {},
        "error": "empty",
    }


async def _read_answer_evidence(
    session: AsyncSession,
    *,
    query: str,
    entry_ids: list[str],
    entry_to_doc: Mapping[str, str],
    evidence_chars: int,
) -> list[dict[str, Any]]:
    if not entry_ids:
        return []
    ctx = ToolContext(session_id="eval-answer", conversation_id="eval-answer")
    metadata = await read_entries_metadata(
        session,
        ctx,
        {"entry_ids": entry_ids, "related_limit": 0},
    )
    metadata_by_id = {
        str(item.get("entry_id")): item
        for item in metadata.get("entries") or []
        if item.get("entry_id")
    }
    patterns = _query_patterns(query)
    read_specs: list[dict[str, Any]] = []
    if patterns:
        read_specs.append({
            "patterns": patterns,
            "context_lines": 2,
            "max_matches": 3,
        })
    read_specs.append({"offset": 0, "max_chars": evidence_chars})
    reads = await read_files(
        session,
        ctx,
        {
            "requests": [
                {
                    "entry_id": eid,
                    "reads": read_specs,
                }
                for eid in entry_ids
            ],
        },
    )
    reads_by_id = {
        str(item.get("entry_id")): item
        for item in reads.get("results") or []
        if item.get("entry_id")
    }

    evidence: list[dict[str, Any]] = []
    for rank, entry_id in enumerate(entry_ids, start=1):
        meta = metadata_by_id.get(entry_id) or {}
        read = reads_by_id.get(entry_id) or {}
        file_meta = meta.get("file") if isinstance(meta.get("file"), dict) else {}
        text = _collect_read_text(read, max_chars=evidence_chars)
        evidence.append({
            "rank": rank,
            "entry_id": entry_id,
            "doc_id": entry_to_doc.get(entry_id),
            "display_name": meta.get("display_name") or entry_id,
            "summary": file_meta.get("summary") or "",
            "description": _compact_description(file_meta.get("description")),
            "text": text,
            "read_ok": bool(text),
            "read_error": read.get("error"),
        })
    return evidence


async def _complete_answer_probe(
    *,
    query: str,
    evidence: list[dict[str, Any]],
    profile: str,
    max_tokens: int,
) -> tuple[str, dict[str, int]]:
    client = get_chat_client(profile)
    resp = await client.complete(ChatRequest(
        system=_ANSWER_PROBE_SYSTEM,
        messages=[
            ChatMessage(
                role="user",
                content=_render_answer_probe_user_prompt(query, evidence),
            ),
        ],
        max_tokens=max_tokens,
        tools=None,
        json_schema=None,
        temperature=0.2,
    ))
    usage = resp.usage
    return resp.text or "", {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


async def _complete_report_probe(
    *,
    query: str,
    evidence: list[dict[str, Any]],
    profile: str,
    max_tokens: int,
) -> tuple[str, dict[str, int]]:
    client = get_chat_client(profile)
    resp = await client.complete(ChatRequest(
        system=_RAG_REPORT_SYSTEM,
        messages=[
            ChatMessage(
                role="user",
                content=_render_report_probe_user_prompt(query, evidence),
            ),
        ],
        max_tokens=max_tokens,
        tools=None,
        json_schema=None,
        temperature=0.2,
    ))
    usage = resp.usage
    return resp.text or "", {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
    }


async def _judge_report_pair(
    *,
    query: str,
    rag_answer: str,
    react_answer: str,
    expected_labels: list[str],
    profile: str,
) -> dict[str, Any]:
    swap = int(hashlib.sha1(query.encode("utf-8")).hexdigest(), 16) % 2 == 1
    answer_a = react_answer if swap else rag_answer
    answer_b = rag_answer if swap else react_answer
    client = get_chat_client(profile)
    try:
        resp = await client.complete(ChatRequest(
            system=_REPORT_JUDGE_SYSTEM,
            messages=[
                ChatMessage(
                    role="user",
                    content=_render_report_judge_prompt(
                        query=query,
                        expected_labels=expected_labels,
                        answer_a=answer_a,
                        answer_b=answer_b,
                    ),
                ),
            ],
            max_tokens=450,
            tools=None,
            json_schema=_REPORT_JUDGE_SCHEMA,
            temperature=0.0,
        ))
        obj = resp.parsed_json or _parse_json_object(resp.text or "")
        raw_winner = str(obj.get("winner") or "tie").lower()
        if raw_winner not in {"a", "b", "tie"}:
            raw_winner = "tie"
        winner = "tie"
        if raw_winner == "a":
            winner = "react" if swap else "rag"
        elif raw_winner == "b":
            winner = "rag" if swap else "react"
        scores_obj = obj.get("scores") if isinstance(obj.get("scores"), Mapping) else {}
        scores = {
            "rag": _coerce_score(scores_obj.get("b" if swap else "a")),
            "react": _coerce_score(scores_obj.get("a" if swap else "b")),
        }
        usage = resp.usage
        return {
            "winner": winner,
            "raw_winner": raw_winner,
            "swapped": swap,
            "scores": scores,
            "reason": str(obj.get("reason") or "")[:800],
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
                "cache_creation_tokens": usage.cache_creation_tokens,
            },
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "winner": "tie",
            "raw_winner": "tie",
            "swapped": swap,
            "scores": {},
            "reason": str(exc)[:800],
            "usage": {},
            "error": repr(exc),
        }


def _render_react_report_user_prompt(query: str) -> str:
    return "\n".join([
        "Investigate the following claim or question using the local knowledge base.",
        "",
        "# Question",
        query.strip(),
        "",
        "# Output",
        "Write a concise investigation report.",
        "If the question is a claim, start with exactly one of:",
        "Verdict: SUPPORT",
        "Verdict: CONTRADICT",
        "Verdict: INSUFFICIENT",
        "",
        "Cover the main supporting evidence, any contradicting or limiting evidence,",
        "and cite factual conclusions with entry_id footnotes.",
    ])


def _render_report_probe_user_prompt(
    query: str,
    evidence: list[dict[str, Any]],
) -> str:
    blocks = [
        "# Question",
        query.strip(),
        "",
        "# Retrieved Evidence",
    ]
    if not evidence:
        blocks.append("(no evidence retrieved)")
    for item in evidence:
        blocks.extend([
            "",
            f"## Candidate {item['rank']}",
            f"doc_id: {item.get('doc_id') or ''}",
            f"entry_id: {item['entry_id']}",
            f"title: {item.get('display_name') or ''}",
        ])
        if item.get("summary"):
            blocks.append(f"summary: {_truncate(str(item['summary']), 700)}")
        if item.get("description"):
            blocks.append(f"description: {_truncate(str(item['description']), 900)}")
        if item.get("text"):
            blocks.extend([
                "source_text:",
                "```",
                str(item["text"]),
                "```",
            ])
        else:
            blocks.append(f"source_text: (not readable: {item.get('read_error') or 'empty'})")
    blocks.extend([
        "",
        "# Task",
        "Write the investigation report now. Do not mention this evaluation harness.",
    ])
    return "\n".join(blocks)


def _render_report_judge_prompt(
    *,
    query: str,
    expected_labels: list[str],
    answer_a: str,
    answer_b: str,
) -> str:
    expected = ", ".join(expected_labels) if expected_labels else "(none supplied)"
    return "\n".join([
        "# User Question",
        query.strip(),
        "",
        "# Gold Verdict",
        expected,
        "",
        "# Answer A",
        answer_a.strip() or "(empty)",
        "",
        "# Answer B",
        answer_b.strip() or "(empty)",
        "",
        "# Judgment Criteria",
        "Prefer the answer that is more useful as a knowledge-base report:",
        "- if a gold verdict is supplied, correctness against it is the first priority",
        "- directly answers the question",
        "- gives a clear conclusion or verdict when applicable",
        "- uses specific evidence and citations",
        "- notes contradictions, uncertainty, or limitations",
        "- avoids unsupported claims and irrelevant detail",
        "",
        "Return JSON only.",
    ])


_RAG_REPORT_SYSTEM = """You are testing a traditional one-shot RAG report path.

Use only the retrieved evidence supplied in the user message. Do not call
tools, infer from outside knowledge, or invent missing facts. If evidence is
insufficient, say that clearly.

If the question is a claim that should be assessed against evidence, start
with exactly one line:
Verdict: SUPPORT
Verdict: CONTRADICT
or
Verdict: INSUFFICIENT

Write a concise investigation report with:
- conclusion
- supporting evidence
- contradicting or limiting evidence when present

Citation rules:
- Cite every factual conclusion using a footnote.
- Footnotes must use this exact shape:
  [^1]: entry_id=<entry_id>, quote="<10-80 copied chars>" - why it supports the answer
- Only cite entry_id values shown in the supplied evidence.
- Do not cite doc_id values; doc_id is evaluation metadata only.
"""


_REPORT_JUDGE_SYSTEM = """You are an impartial evaluator comparing two report answers.

You do not know which system produced each answer. Judge only the report
quality for the given user question. Prefer correctness, evidence use,
citation quality, uncertainty handling, and completeness over style.
Return strict JSON only.
"""


_REPORT_JUDGE_SCHEMA: dict[str, Any] = {
    "title": "ReportPairJudge",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "winner": {
            "type": "string",
            "enum": ["a", "b", "tie"],
        },
        "scores": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "a": {"type": "number", "minimum": 0, "maximum": 10},
                "b": {"type": "number", "minimum": 0, "maximum": 10},
            },
            "required": ["a", "b"],
        },
        "reason": {"type": "string"},
    },
    "required": ["winner", "scores", "reason"],
}


_ANSWER_PROBE_SYSTEM = """You are testing Marginalia's final-answer path.

Answer the question only from the evidence supplied in the user message. If
the evidence is insufficient, say that clearly. Keep the answer concise and
use Markdown.

If the question is a claim that should be assessed against evidence, start
with exactly one line:
Verdict: SUPPORT
Verdict: CONTRADICT
or
Verdict: INSUFFICIENT

Citation rules:
- Cite every factual conclusion using a footnote.
- Footnotes must use this exact shape:
  [^1]: entry_id=<entry_id>, quote="<10-80 copied chars>" - why it supports the answer
- Only cite entry_id values shown in the supplied evidence.
- Do not cite doc_id values; doc_id is evaluation metadata only.
"""


def _render_answer_probe_user_prompt(
    query: str,
    evidence: list[dict[str, Any]],
) -> str:
    blocks = [
        "# Question",
        query.strip(),
        "",
        "# Retrieved Evidence",
    ]
    if not evidence:
        blocks.append("(no evidence retrieved)")
    for item in evidence:
        blocks.extend([
            "",
            f"## Candidate {item['rank']}",
            f"doc_id: {item.get('doc_id') or ''}",
            f"entry_id: {item['entry_id']}",
            f"title: {item.get('display_name') or ''}",
        ])
        if item.get("summary"):
            blocks.append(f"summary: {_truncate(str(item['summary']), 700)}")
        if item.get("description"):
            blocks.append(f"description: {_truncate(str(item['description']), 900)}")
        if item.get("text"):
            blocks.extend([
                "source_text:",
                "```",
                str(item["text"]),
                "```",
            ])
        else:
            blocks.append(f"source_text: (not readable: {item.get('read_error') or 'empty'})")
    blocks.extend([
        "",
        "# Task",
        "Write the final answer now. Do not mention this evaluation harness.",
    ])
    return "\n".join(blocks)


def _expected_labels(
    metadata: Mapping[str, Any],
    relevant_doc_ids: Iterable[str],
) -> list[str]:
    labels: list[str] = []
    for doc_id in relevant_doc_ids:
        raw = metadata.get(doc_id)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            label = str(item.get("label") or "").strip().upper()
            if label in {"SUPPORT", "CONTRADICT", "INSUFFICIENT"}:
                _append_unique_str(labels, label)
    return labels


_VERDICT_RE = re.compile(
    r"^\s*(?:\*\*)?\s*Verdict\s*:\s*"
    r"(SUPPORT|CONTRADICT|INSUFFICIENT)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _predict_answer_label(answer: str) -> str | None:
    match = _VERDICT_RE.search(answer or "")
    if match:
        return match.group(1).upper()
    text = (answer or "").casefold()
    if "insufficient" in text or "cannot be verified" in text or "not possible to" in text:
        return "INSUFFICIENT"
    if "contradict" in text or "incorrect" in text or "does not support" in text:
        return "CONTRADICT"
    if "support" in text or "accurate" in text or "consistent with" in text:
        return "SUPPORT"
    return None


def _append_unique_str(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


_CITED_ENTRY_RE = re.compile(
    r"(?:entry_id\s*=\s*`?|entry:)"
    r"([0-9a-fA-F][0-9a-fA-F\-]{6,35})`?"
)

_QUERY_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+_./,-]*")
_QUERY_STOPWORDS = {
    "about",
    "after",
    "against",
    "and",
    "are",
    "consisting",
    "does",
    "from",
    "have",
    "into",
    "larger",
    "than",
    "that",
    "the",
    "their",
    "this",
    "with",
}


def _extract_cited_entry_ids(
    answer: str,
    *,
    known_entry_ids: Iterable[str],
) -> list[str]:
    known = list(known_entry_ids)
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITED_ENTRY_RE.finditer(answer or ""):
        resolved = _resolve_cited_entry_id(match.group(1), known)
        if resolved is None or resolved in seen:
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def _resolve_cited_entry_id(raw: str, known_entry_ids: list[str]) -> str | None:
    raw_clean = raw.strip().strip("`")
    if raw_clean in known_entry_ids:
        return raw_clean
    compact = raw_clean.replace("-", "").lower()
    matches = [
        eid for eid in known_entry_ids
        if eid.replace("-", "").lower().startswith(compact)
    ]
    return matches[0] if len(matches) == 1 else None


def _parse_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _coerce_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(score) or math.isinf(score):
        return None
    return max(0.0, min(10.0, score))


def _query_patterns(query: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in _QUERY_TOKEN_RE.findall(query):
        token = raw.strip(".,;:!?()[]{}\"'")
        if not token:
            continue
        key = token.casefold()
        if key in seen or key in _QUERY_STOPWORDS:
            continue
        has_digit = any(ch.isdigit() for ch in token)
        has_upper = any(ch.isupper() for ch in token)
        if len(token) < 4 and not has_digit and not has_upper:
            continue
        seen.add(key)
        out.append(re.escape(token))
        if len(out) >= 8:
            break
    return out


def _collect_read_text(read: Mapping[str, Any], *, max_chars: int) -> str:
    parts: list[str] = []
    used = 0
    for segment in read.get("reads") or []:
        if not isinstance(segment, Mapping):
            continue
        text = segment.get("text")
        if not segment.get("ok") or not text:
            continue
        label = _read_label(segment.get("args") or {})
        block = f"{label}\n{text}" if label else str(text)
        remaining = max_chars - used
        if remaining <= 0:
            break
        parts.append(block[:remaining])
        used += min(len(block), remaining)
    return "\n\n".join(parts).strip()


def _read_label(args: Any) -> str:
    if not isinstance(args, Mapping):
        return ""
    if args.get("pattern"):
        return f"[pattern: {args['pattern']}]"
    if args.get("line_start"):
        end = args.get("line_end") or args.get("line_start")
        return f"[lines {args['line_start']}-{end}]"
    if args.get("offset"):
        return f"[offset {args['offset']}]"
    return "[start]"


def _compact_description(value: Any) -> str:
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        sections = value.get("sections")
        if isinstance(sections, list):
            parts = []
            for section in sections[:5]:
                if not isinstance(section, dict):
                    continue
                title = str(section.get("title") or "").strip()
                summary = str(section.get("summary") or "").strip()
                if title or summary:
                    parts.append(f"{title}: {summary}".strip(": "))
            return "; ".join(parts)
    if isinstance(value, str):
        return value.strip()
    return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...(truncated)"


async def _retrieve_entries(
    session: AsyncSession,
    *,
    retriever: str,
    query: str,
    limit: int,
) -> list[str]:
    ctx = ToolContext(session_id="eval", conversation_id="eval")
    if retriever == "search_metadata":
        result = await search_metadata(
            session,
            ctx,
            {"text": query, "limit": limit},
        )
        return [str(e["entry_id"]) for e in result.get("entries") or []]
    if retriever == "semantic_recall":
        rows = await semantic_entry_rows(session, query, limit=limit)
        return [str(e["entry_id"]) for e in rows]
    if retriever == "recall_knowledge":
        result = await recall_knowledge(
            session,
            ctx,
            {"text": query, "limit": limit},
        )
        return [str(eid) for eid in result.get("verify_entry_ids") or []]
    raise ValueError(
        "unknown retriever "
        f"{retriever!r}; expected search_metadata, semantic_recall, or recall_knowledge"
    )


async def _retrieve_entries_many(
    session: AsyncSession,
    *,
    retriever: str,
    queries: list[str],
    limit: int,
) -> list[list[str]]:
    details = await _retrieve_entries_many_detail(
        session,
        retriever=retriever,
        queries=queries,
        limit=limit,
        evidence_limit=None,
    )
    return [detail["ranked_ids"] for detail in details]


async def _retrieve_entries_many_detail(
    session: AsyncSession,
    *,
    retriever: str,
    queries: list[str],
    limit: int,
    evidence_limit: int | None,
) -> list[dict[str, list[str]]]:
    if not queries:
        return []
    if retriever == "recall_knowledge":
        return await _retrieve_recall_knowledge_many(
            session,
            queries=queries,
            limit=limit,
            evidence_limit=evidence_limit,
        )
    ranked_many = [
        await _retrieve_entries(
            session,
            retriever=retriever,
            query=query,
            limit=limit,
        )
        for query in queries
    ]
    return [
        {
            "ranked_ids": ranked,
            "evidence_ids": ranked[:evidence_limit] if evidence_limit else ranked,
        }
        for ranked in ranked_many
    ]


async def _retrieve_recall_knowledge_many(
    session: AsyncSession,
    *,
    queries: list[str],
    limit: int,
    evidence_limit: int | None,
) -> list[dict[str, list[str]]]:
    fetch_limit = 100
    settings = get_settings()
    text_terms_by_query = [normalize_text_queries(query) for query in queries]
    semantic_queries = [" ".join(terms) for terms in text_terms_by_query]
    semantic_hits_many = (
        await search_semantic_index_many(
            semantic_queries,
            limit=min(fetch_limit, settings.semantic_recall_limit),
        )
        if semantic_recall_configured()
        else [[] for _query in queries]
    )
    semantic_ids = sorted({
        hit.entry_id
        for hits in semantic_hits_many
        for hit in hits
    })
    semantic_rows_by_id = await _entry_rows_by_id(session, semantic_ids)
    metadata_results = await _search_metadata_many(
        text_terms_by_query,
        limit=fetch_limit,
        concurrency=20,
    )

    ranked_by_query: list[list[dict[str, Any]]] = []
    queries_for_rerank: list[str] = []
    rerank_entry_ids: list[str] = []
    rerank_top_n = max(1, int(settings.rerank_top_n or 80))
    for text_terms, metadata_entries, semantic_hits in zip(
        text_terms_by_query,
        metadata_results,
        semantic_hits_many,
    ):
        entry_map: dict[str, dict[str, Any]] = {}
        _merge_eval_entries(entry_map, metadata_entries, "metadata_text")
        semantic_entries = [
            semantic_rows_by_id[hit.entry_id]
            for hit in semantic_hits
            if hit.entry_id in semantic_rows_by_id
        ]
        _merge_eval_entries(entry_map, semantic_entries, "semantic")
        ranked = score_recall_entries(list(entry_map.values()), text_terms=text_terms)
        ranked_by_query.append(ranked)
        queries_for_rerank.append(" ".join(text_terms))
        if text_terms and rerank_configured(settings):
            for row in ranked[:rerank_top_n]:
                entry_id = str(row.get("entry_id") or "")
                if entry_id:
                    rerank_entry_ids.append(entry_id)

    if rerank_entry_ids and rerank_configured(settings):
        documents_by_id = await load_rerank_documents_by_entry_id(session, rerank_entry_ids)
        semaphore = asyncio.Semaphore(max(1, int(settings.rerank_concurrency or 10)))

        async def _rerank_one(
            query: str,
            ranked: list[dict[str, Any]],
        ) -> list[dict[str, Any]]:
            if not query.strip() or not ranked:
                return ranked
            async with semaphore:
                reranked, _trace = await rerank_recall_entries_with_documents(
                    ranked,
                    query=query,
                    documents_by_id=documents_by_id,
                )
                return reranked

        ranked_by_query = await asyncio.gather(*(
            _rerank_one(query, ranked)
            for query, ranked in zip(queries_for_rerank, ranked_by_query)
        ))

    out: list[dict[str, list[str]]] = []
    for ranked in ranked_by_query:
        ranked_ids = [
            str(entry.get("entry_id"))
            for entry in ranked[:max(1, limit)]
            if entry.get("entry_id")
        ]
        evidence_ids = (
            select_evidence_entry_ids(ranked[:max(1, limit)], max(1, evidence_limit))
            if evidence_limit
            else ranked_ids
        )
        out.append({"ranked_ids": ranked_ids, "evidence_ids": evidence_ids})
    return out


async def _search_metadata_many(
    text_terms_by_query: list[list[str]],
    *,
    limit: int,
    concurrency: int,
) -> list[list[Any]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(text_terms: list[str]) -> list[Any]:
        if not text_terms:
            return []
        async with semaphore:
            async with session_scope() as session:
                result = await search_metadata(
                    session,
                    ToolContext(session_id="eval", conversation_id="eval"),
                    {"text": text_terms, "limit": limit},
                )
                return list(result.get("entries") or [])

    return await asyncio.gather(*(_one(text_terms) for text_terms in text_terms_by_query))


async def _entry_rows_by_id(
    session: AsyncSession,
    entry_ids: list[str],
) -> dict[str, dict[str, Any]]:
    rows = await entries_repo.list_live_with_file_by_ids(session, entry_ids)
    out: dict[str, dict[str, Any]] = {}
    for entry, file_row in rows:
        out[entry.id] = {
            "entry_id": entry.id,
            "display_name": entry.display_name,
            "lifecycle": entry.lifecycle,
            "kind": file_row.kind,
            "summary": file_row.summary,
            "catalog_id": entry.catalog_id,
            "folder_id": entry.folder_id,
        }
    return out


def _merge_eval_entries(
    entry_map: dict[str, dict[str, Any]],
    entries: list[Any],
    source: str,
) -> None:
    total = len(entries)
    for idx, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        entry_id = str(entry.get("entry_id") or "")
        if not entry_id:
            continue
        existing = entry_map.get(entry_id)
        if existing is None:
            existing = {
                "entry_id": entry_id,
                "display_name": entry.get("display_name"),
                "lifecycle": entry.get("lifecycle"),
                "kind": entry.get("kind"),
                "summary": entry.get("summary"),
                "catalog_id": entry.get("catalog_id"),
                "folder_id": entry.get("folder_id"),
                "coverage": entry.get("coverage"),
                "matched_by": [],
                "rrf_score": 0.0,
                "rank_score": 0,
                "score": 0.0,
                "score_components": {},
            }
            entry_map[entry_id] = existing
        else:
            for key in (
                "display_name",
                "lifecycle",
                "kind",
                "summary",
                "catalog_id",
                "folder_id",
                "coverage",
            ):
                if existing.get(key) in (None, "") and entry.get(key) not in (None, ""):
                    existing[key] = entry.get(key)
        _append_unique_str(existing["matched_by"], source)
        rank_key = _rank_key_for_source(source)
        if rank_key:
            rank = idx + 1
            existing[rank_key] = min(
                int(existing.get(rank_key) or rank),
                rank,
            )
        existing["rank_score"] = max(
            int(existing.get("rank_score") or 0),
            total - idx,
        )
        existing["rrf_score"] = _eval_rrf_score(existing)


def _rank_key_for_source(source: str) -> str | None:
    if source in {"metadata_text", "metadata_tags"}:
        return "lexical_rank"
    if source == "semantic":
        return "semantic_rank"
    return None


def _eval_rrf_score(row: Mapping[str, Any], *, k: int = 60) -> float:
    score = 0.0
    for key in ("lexical_rank", "semantic_rank"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            rank = int(raw)
        except (TypeError, ValueError):
            continue
        if rank > 0:
            score += 1.0 / (k + rank)
    return score


def _eval_entry_sort_key(row: Mapping[str, Any]) -> tuple[float, int, int, str]:
    matched_by = set(row.get("matched_by") or [])
    return (
        -float(row.get("rrf_score") or 0.0),
        -int("metadata_text" in matched_by and "semantic" in matched_by),
        -int(row.get("rank_score") or 0),
        str(row.get("display_name") or ""),
    )


def _select_quota_evidence_ids(
    ranked: list[Mapping[str, Any]],
    evidence_limit: int,
) -> list[str]:
    if evidence_limit <= 0:
        return []
    overlap_quota, lexical_quota, semantic_quota = _evidence_quotas(evidence_limit)
    overlap: list[Mapping[str, Any]] = []
    lexical_only: list[Mapping[str, Any]] = []
    semantic_only: list[Mapping[str, Any]] = []
    for row in ranked:
        matched_by = set(row.get("matched_by") or [])
        has_lexical = "metadata_text" in matched_by or "metadata_tags" in matched_by
        has_semantic = "semantic" in matched_by
        if has_lexical and has_semantic:
            overlap.append(row)
        elif has_lexical:
            lexical_only.append(row)
        elif has_semantic:
            semantic_only.append(row)

    out: list[str] = []
    seen: set[str] = set()

    def take(rows: list[Mapping[str, Any]], quota: int) -> None:
        for row in rows:
            if len(out) >= evidence_limit or quota <= 0:
                return
            entry_id = str(row.get("entry_id") or "")
            if not entry_id or entry_id in seen:
                continue
            seen.add(entry_id)
            out.append(entry_id)
            quota -= 1

    take(overlap, overlap_quota)
    take(lexical_only, lexical_quota)
    take(semantic_only, semantic_quota)
    take(ranked, evidence_limit - len(out))
    return out[:evidence_limit]


def _evidence_quotas(evidence_limit: int) -> tuple[int, int, int]:
    if evidence_limit <= 1:
        return evidence_limit, 0, 0
    overlap = max(1, round(evidence_limit * 0.4))
    lexical = max(1, round(evidence_limit * 0.4))
    semantic = max(0, evidence_limit - overlap - lexical)
    return overlap, lexical, semantic


def _score_query(
    ranked_doc_ids: list[str],
    relevant: Mapping[str, int],
    ks: list[int],
) -> dict[str, Any]:
    rel_set = set(relevant)
    first_rank = next(
        (idx + 1 for idx, doc_id in enumerate(ranked_doc_ids) if doc_id in rel_set),
        None,
    )
    hit: dict[str, float] = {}
    recall: dict[str, float] = {}
    ndcg: dict[str, float] = {}
    for k in ks:
        top = ranked_doc_ids[:k]
        hits = len(set(top).intersection(rel_set))
        hit[str(k)] = 1.0 if hits else 0.0
        recall[str(k)] = hits / len(rel_set)
        ndcg[str(k)] = _ndcg_at_k(top, relevant, k)
    return {
        "first_relevant_rank": first_rank,
        "mrr": 1.0 / first_rank if first_rank else 0.0,
        "hit": hit,
        "recall": recall,
        "ndcg": ndcg,
    }


def _ndcg_at_k(
    ranked_doc_ids: list[str],
    relevant: Mapping[str, int],
    k: int,
) -> float:
    def gain(rel: int) -> float:
        return (2.0 ** rel) - 1.0

    dcg = 0.0
    for idx, doc_id in enumerate(ranked_doc_ids[:k], start=1):
        rel = relevant.get(doc_id, 0)
        if rel <= 0:
            continue
        dcg += gain(rel) / math.log2(idx + 1)
    ideal = sorted((rel for rel in relevant.values() if rel > 0), reverse=True)[:k]
    idcg = sum(gain(rel) / math.log2(idx + 1) for idx, rel in enumerate(ideal, start=1))
    return dcg / idcg if idcg else 0.0


class _MetricAccumulator:
    def __init__(self, ks: list[int]) -> None:
        self.ks = ks
        self.evaluated = 0
        self.skipped = 0
        self.zero_results = 0
        self.no_relevant_at_max_k = 0
        self.mrr = 0.0
        self.hit_rate = {k: 0.0 for k in ks}
        self.recall = {k: 0.0 for k in ks}
        self.ndcg = {k: 0.0 for k in ks}

    def add(self, scored: Mapping[str, Any], *, zero_result: bool) -> None:
        self.evaluated += 1
        if zero_result:
            self.zero_results += 1
        if not scored.get("first_relevant_rank"):
            self.no_relevant_at_max_k += 1
        self.mrr += float(scored.get("mrr") or 0.0)
        scored_hit = scored.get("hit") or {}
        scored_recall = scored.get("recall") or {}
        scored_ndcg = scored.get("ndcg") or {}
        for k in self.ks:
            self.hit_rate[k] += float(scored_hit.get(str(k)) or 0.0)
            self.recall[k] += float(scored_recall.get(str(k)) or 0.0)
            self.ndcg[k] += float(scored_ndcg.get(str(k)) or 0.0)

    def result(
        self,
        *,
        name: str,
        retriever: str,
        queries_total: int,
        per_query: list[dict[str, Any]],
    ) -> EvalRunResult:
        denom = max(1, self.evaluated)
        return EvalRunResult(
            name=name,
            retriever=retriever,
            queries_total=queries_total,
            queries_evaluated=self.evaluated,
            queries_skipped=self.skipped,
            zero_result_rate=self.zero_results / denom,
            no_relevant_at_k_rate=self.no_relevant_at_max_k / denom,
            mrr=self.mrr / denom,
            hit_rate={k: self.hit_rate[k] / denom for k in self.ks},
            recall={k: self.recall[k] / denom for k in self.ks},
            ndcg={k: self.ndcg[k] / denom for k in self.ks},
            per_query=per_query,
        )


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _render_document(doc: BeirDocument) -> str:
    lines = [f"Document ID: {doc.doc_id}"]
    if doc.title.strip():
        lines.extend(["", f"# {doc.title.strip()}"])
    if doc.metadata:
        lines.extend(["", "Metadata:", json.dumps(doc.metadata, ensure_ascii=False)])
    if doc.text.strip():
        lines.extend(["", doc.text.strip()])
    return "\n".join(lines).strip() + "\n"


def _doc_display_name(doc_id: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in doc_id)
    safe = safe.strip("._") or hashlib.sha1(doc_id.encode("utf-8")).hexdigest()[:16]
    if not safe.lower().endswith(".txt"):
        safe += ".txt"
    return safe[:255]


def _resolve_qrels_path(source_dir: Path, *, split: str) -> Path:
    direct = source_dir / "qrels.tsv"
    if direct.exists():
        return direct
    split_path = source_dir / "qrels" / f"{split}.tsv"
    if split_path.exists():
        return split_path
    qrels_dir = source_dir / "qrels"
    if qrels_dir.is_dir():
        candidates = sorted(qrels_dir.glob("*.tsv"))
        if len(candidates) == 1:
            return candidates[0]
    return split_path


def _require_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(path)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
