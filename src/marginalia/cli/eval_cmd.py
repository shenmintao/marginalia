"""`marginalia eval ...` command group."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from marginalia.db.engine import dispose_engine
from marginalia.eval.core import (
    answer_probe_to_dict,
    answer_run_to_dict,
    build_eval_semantic_index,
    format_report_compare_result,
    format_answer_run_result,
    format_answer_probe_result,
    format_run_result,
    import_beir_dataset,
    report_compare_to_dict,
    result_to_dict,
    run_answer_eval_dataset,
    run_answer_probe,
    run_eval_dataset,
    run_report_compare_dataset,
)


def cmd_eval_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="marginalia eval",
        description="Import external retrieval datasets and run retrieval metrics.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_import = sub.add_parser(
        "import-beir",
        help="Import a local BEIR-style dataset and synchronously ingest corpus docs.",
    )
    p_import.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_import.add_argument("source_dir", help="Directory containing corpus.jsonl, queries.jsonl, qrels/")
    p_import.add_argument("--split", default="test", help="qrels split name, default: test")
    p_import.add_argument("--limit", type=int, default=None, help="Import only first N corpus docs")
    p_import.add_argument(
        "--remote-folder",
        default=None,
        help="Library folder to place eval docs in, default: /eval/<name>/",
    )
    p_import.add_argument(
        "--progress-every",
        type=int,
        default=25,
        help="Print progress every N imported docs; 0 disables progress lines.",
    )
    p_import.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent corpus document ingests, default: 1.",
    )
    p_import.add_argument(
        "--resume",
        action="store_true",
        help="Continue a partial import in an existing eval dataset directory.",
    )

    p_semantic = sub.add_parser(
        "build-semantic-index",
        help="Build a local semantic index for an imported eval dataset.",
    )
    p_semantic.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_semantic.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Embedding batch size, default: EMBEDDING_BATCH_SIZE.",
    )
    p_semantic.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent embedding batch requests, default: 1.",
    )
    p_semantic.add_argument(
        "--resume",
        action="store_true",
        help="Resume from entries.jsonl.tmp/vectors.f32.tmp after an interrupted build.",
    )
    p_semantic.add_argument(
        "--progress-every",
        type=int,
        default=50,
        help="Print progress every N indexed entries; 0 disables progress lines.",
    )

    p_run = sub.add_parser("run", help="Run retrieval metrics for an imported dataset.")
    p_run.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_run.add_argument(
        "--retriever",
        choices=("search_metadata", "semantic_recall", "recall_knowledge"),
        default="search_metadata",
    )
    p_run.add_argument(
        "--k",
        default="10,50,100",
        help="Comma-separated candidate cutoffs, default: 10,50,100",
    )
    p_run.add_argument("--query-limit", type=int, default=None)
    p_run.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Optional path to write the full JSON report.",
    )

    p_answer = sub.add_parser(
        "answer",
        help="Generate one bounded final answer from retrieved eval evidence.",
    )
    p_answer.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_answer.add_argument(
        "--retriever",
        choices=("search_metadata", "semantic_recall", "recall_knowledge"),
        default="recall_knowledge",
    )
    p_answer.add_argument("--query-id", default=None)
    p_answer.add_argument(
        "--query",
        default=None,
        help="Ad hoc query text; when omitted, --query-id or first qrels query is used.",
    )
    p_answer.add_argument(
        "--retrieval-limit",
        type=int,
        default=20,
        help="Candidate pool size before evidence reads, default: 20.",
    )
    p_answer.add_argument(
        "--evidence-limit",
        type=int,
        default=10,
        help="How many retrieved candidates to read, default: 10.",
    )
    p_answer.add_argument(
        "--evidence-chars",
        type=int,
        default=2000,
        help="Characters to read from each evidence entry, default: 2000.",
    )
    p_answer.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Hard wall-clock timeout for this answer probe, default: 300.",
    )
    p_answer.add_argument(
        "--max-tokens",
        type=int,
        default=700,
        help="Final-answer output token budget, default: 700.",
    )
    p_answer.add_argument(
        "--profile",
        default="chat",
        help="LLM profile used for final answer generation, default: chat.",
    )
    p_answer.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Optional path to write the full JSON report.",
    )

    p_answer_run = sub.add_parser(
        "answer-run",
        help="Run bounded final-answer probes for imported eval queries.",
    )
    p_answer_run.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_answer_run.add_argument(
        "--retriever",
        choices=("search_metadata", "semantic_recall", "recall_knowledge"),
        default="recall_knowledge",
    )
    p_answer_run.add_argument(
        "--retrieval-limit",
        type=int,
        default=20,
        help="Candidate pool size before evidence reads, default: 20.",
    )
    p_answer_run.add_argument(
        "--evidence-limit",
        type=int,
        default=10,
        help="How many retrieved candidates to read per query, default: 10.",
    )
    p_answer_run.add_argument(
        "--evidence-chars",
        type=int,
        default=2000,
        help="Characters to read from each evidence entry, default: 2000.",
    )
    p_answer_run.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Hard wall-clock timeout per query, default: 300.",
    )
    p_answer_run.add_argument(
        "--max-tokens",
        type=int,
        default=700,
        help="Final-answer output token budget per query, default: 700.",
    )
    p_answer_run.add_argument(
        "--profile",
        default="chat",
        help="LLM profile used for final answer generation, default: chat.",
    )
    p_answer_run.add_argument("--query-limit", type=int, default=None)
    p_answer_run.add_argument(
        "--qrels-only",
        action="store_true",
        help="Apply --query-limit after keeping only queries with imported qrels.",
    )
    p_answer_run.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent final-answer probes, default: 1.",
    )
    p_answer_run.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Optional path to write the full JSON report.",
    )

    p_compare = sub.add_parser(
        "compare-report",
        help="Compare one-shot RAG reports with the full ReAct report workflow.",
    )
    p_compare.add_argument("name", help="Dataset name under MARGINALIA_HOME/eval/")
    p_compare.add_argument(
        "--retriever",
        choices=("search_metadata", "semantic_recall", "recall_knowledge"),
        default="recall_knowledge",
    )
    p_compare.add_argument(
        "--retrieval-limit",
        type=int,
        default=20,
        help="RAG candidate pool size before evidence reads, default: 20.",
    )
    p_compare.add_argument(
        "--evidence-limit",
        type=int,
        default=10,
        help="How many retrieved candidates one-shot RAG may read, default: 10.",
    )
    p_compare.add_argument(
        "--evidence-chars",
        type=int,
        default=2000,
        help="Characters to read from each RAG evidence entry, default: 2000.",
    )
    p_compare.add_argument(
        "--timeout-seconds",
        type=float,
        default=300.0,
        help="Hard wall-clock timeout per query for RAG+ReAct+judge, default: 300.",
    )
    p_compare.add_argument(
        "--max-tokens",
        type=int,
        default=900,
        help="One-shot RAG report output token budget, default: 900.",
    )
    p_compare.add_argument(
        "--profile",
        default="chat",
        help="LLM profile for one-shot RAG report generation, default: chat.",
    )
    p_compare.add_argument(
        "--judge-profile",
        default="chat",
        help="LLM profile for blind pairwise judging, default: chat.",
    )
    p_compare.add_argument("--query-limit", type=int, default=30)
    p_compare.add_argument(
        "--qrels-only",
        action="store_true",
        default=True,
        help="Keep only queries with imported qrels before applying --query-limit.",
    )
    p_compare.add_argument(
        "--all-queries",
        action="store_false",
        dest="qrels_only",
        help="Allow queries without imported positive qrels.",
    )
    p_compare.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Concurrent report comparisons, default: 1.",
    )
    p_compare.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="Optional path to write the full JSON report.",
    )

    args = parser.parse_args(argv)
    try:
        if args.cmd == "import-beir":
            return asyncio.run(_run_import(args))
        if args.cmd == "build-semantic-index":
            return asyncio.run(_run_build_semantic_index(args))
        if args.cmd == "run":
            return asyncio.run(_run_eval(args))
        if args.cmd == "answer":
            return asyncio.run(_run_answer(args))
        if args.cmd == "answer-run":
            return asyncio.run(_run_answer_run(args))
        if args.cmd == "compare-report":
            return asyncio.run(_run_compare_report(args))
    except KeyboardInterrupt:
        return 130
    return 2


async def _run_import(args: argparse.Namespace) -> int:
    try:
        result = await import_beir_dataset(
            name=args.name,
            source_dir=Path(args.source_dir),
            split=args.split,
            limit=args.limit,
            remote_folder=args.remote_folder,
            progress_every=args.progress_every,
            concurrency=args.concurrency,
            resume=args.resume,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval import failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(
        f"imported {result.docs_imported} document(s) into eval dataset "
        f"{result.name!r}"
    )
    print(f"  dataset_dir: {result.dataset_dir}")
    print(f"  queries: {result.queries}  qrels: {result.qrels}  split: {result.split}")
    print(f"  concurrency: {result.concurrency}  resumed: {result.resumed}")
    return 0


async def _run_build_semantic_index(args: argparse.Namespace) -> int:
    try:
        result = await build_eval_semantic_index(
            name=args.name,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
            resume=args.resume,
            progress_every=args.progress_every,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval build-semantic-index failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(f"semantic index built for eval dataset {args.name!r}")
    print(f"  index_dir: {result.index_dir}")
    print(f"  entries: {result.entries_indexed}")
    print(f"  model: {result.model}  dimensions: {result.dimensions}")
    print(f"  concurrency: {args.concurrency}  resumed: {args.resume}")
    print(f"  elapsed_ms: {result.elapsed_ms}  total_tokens: {result.total_tokens}")
    return 0


async def _run_eval(args: argparse.Namespace) -> int:
    try:
        k_values = _parse_k_values(args.k)
        result = await run_eval_dataset(
            name=args.name,
            retriever=args.retriever,
            k_values=k_values,
            query_limit=args.query_limit,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval run failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(format_run_result(result))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(result_to_dict(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\njson_report: {out}")
    return 0


async def _run_answer(args: argparse.Namespace) -> int:
    try:
        result = await run_answer_probe(
            name=args.name,
            retriever=args.retriever,
            query_id=args.query_id,
            query=args.query,
            retrieval_limit=args.retrieval_limit,
            evidence_limit=args.evidence_limit,
            evidence_chars=args.evidence_chars,
            timeout_seconds=args.timeout_seconds,
            max_tokens=args.max_tokens,
            profile=args.profile,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval answer failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(format_answer_probe_result(result))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(answer_probe_to_dict(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\njson_report: {out}")
    return 0


async def _run_answer_run(args: argparse.Namespace) -> int:
    try:
        result = await run_answer_eval_dataset(
            name=args.name,
            retriever=args.retriever,
            retrieval_limit=args.retrieval_limit,
            evidence_limit=args.evidence_limit,
            evidence_chars=args.evidence_chars,
            timeout_seconds=args.timeout_seconds,
            max_tokens=args.max_tokens,
            profile=args.profile,
            query_limit=args.query_limit,
            qrels_only=args.qrels_only,
            concurrency=args.concurrency,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval answer-run failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(format_answer_run_result(result))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(answer_run_to_dict(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\njson_report: {out}")
    return 0


async def _run_compare_report(args: argparse.Namespace) -> int:
    try:
        result = await run_report_compare_dataset(
            name=args.name,
            retriever=args.retriever,
            retrieval_limit=args.retrieval_limit,
            evidence_limit=args.evidence_limit,
            evidence_chars=args.evidence_chars,
            timeout_seconds=args.timeout_seconds,
            max_tokens=args.max_tokens,
            profile=args.profile,
            judge_profile=args.judge_profile,
            query_limit=args.query_limit,
            qrels_only=args.qrels_only,
            concurrency=args.concurrency,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"eval compare-report failed: {exc}")
        return 1
    finally:
        await dispose_engine()

    print(format_report_compare_result(result))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report_compare_to_dict(result), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\njson_report: {out}")
    return 0


def _parse_k_values(raw: str) -> list[int]:
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        value = int(part)
        if value > 0:
            out.append(value)
    return out or [10]
