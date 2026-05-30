from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from marginalia.llm.types import ChatRequest, ChatResponse, TokenUsage


class _FakeIngestClient:
    profile_name = "ingest"
    model = "fake-ingest"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        text = "\n".join(str(m.content or "") for m in request.messages)
        doc_text = _between(text, "<document>", "</document>") or text
        summary = " ".join(doc_text.split())[:220]
        return ChatResponse(
            text=(
                f"<summary>{summary}</summary>\n"
                f"<description>{summary}</description>\n"
                "<sections>\n"
                f"s1 | lines 1-20 | Document | {summary} | {summary}\n"
                "</sections>\n"
                "<extra>\n"
                f"retrieval_terms: {summary}\n"
                "</extra>\n"
                "<entry_extra></entry_extra>\n"
                "<catalog_path>Eval</catalog_path>\n"
                "<tags>\n"
                "topic: raft, consensus\n"
                "form: eval\n"
                "language: en\n"
                "</tags>\n"
            ),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=100, output_tokens=50),
            parsed_json=None,
        )


class _FakeAnswerClient:
    profile_name = "chat"
    provider = "openai"
    model = "fake-answer"

    async def complete(self, request: ChatRequest) -> ChatResponse:
        text = "\n".join(str(m.content or "") for m in request.messages)
        match = re.search(r"entry_id:\s*([0-9a-fA-F-]{36})", text)
        entry_id = match.group(1) if match else "00000000-0000-0000-0000-000000000000"
        return ChatResponse(
            text=(
                "Verdict: SUPPORT\n\n"
                "Raft uses leader election as part of consensus. [^1]\n\n"
                f"[^1]: entry_id={entry_id}, quote=\"leader election\" - "
                "The retrieved source states the key evidence."
            ),
            tool_calls=[],
            stop_reason="end_turn",
            usage=TokenUsage(input_tokens=200, output_tokens=60),
            parsed_json=None,
        )


def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    j = text.find(end, i + len(start))
    if i < 0 or j < 0:
        return ""
    return text[i + len(start):j]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_import_beir_runs_ingest_and_eval_metrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("MARGINALIA_HOME", str(home))
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("WORKER_ENABLED", "false")
    monkeypatch.setenv("LLM_DEFAULT_API_KEY", "sk-fake")
    monkeypatch.setenv("LLM_DEFAULT_MODEL", "fake-model")

    from marginalia.config import get_settings
    from marginalia.db.engine import dispose_engine
    from marginalia.storage import reset_storage_cache

    get_settings.cache_clear()  # type: ignore[attr-defined]
    reset_storage_cache()
    await dispose_engine()

    import marginalia.pipelines.text as text_pipeline

    monkeypatch.setattr(
        text_pipeline,
        "get_chat_client",
        lambda profile="ingest": _FakeIngestClient(),
    )

    source = tmp_path / "beir"
    (source / "qrels").mkdir(parents=True)
    _write_jsonl(
        source / "corpus.jsonl",
        [
            {
                "_id": "d1",
                "title": "Raft paper",
                "text": "Raft consensus uses leader election and replicated logs.",
            },
            {
                "_id": "d2",
                "title": "Cooking notes",
                "text": "A sourdough starter needs flour and water.",
            },
        ],
    )
    _write_jsonl(
        source / "queries.jsonl",
        [
            {"_id": "q0", "text": "unjudged query"},
            {
                "_id": "q1",
                "text": "raft leader election",
                "metadata": {"d1": [{"label": "SUPPORT"}]},
            }
        ],
    )
    (source / "qrels" / "test.tsv").write_text(
        "query-id\tcorpus-id\tscore\nq1\td1\t1\n",
        encoding="utf-8",
    )

    import marginalia.eval.core as eval_core

    imported = await eval_core.import_beir_dataset(
        name="tiny",
        source_dir=source,
        progress_every=0,
    )
    assert imported.docs_imported == 2
    assert imported.concurrency == 1
    assert imported.resumed is False

    result = await eval_core.run_eval_dataset(
        name="tiny",
        retriever="search_metadata",
        k_values=[1, 2],
    )
    assert result.queries_evaluated == 1
    assert result.recall[1] == 1.0, result.per_query
    assert result.hit_rate[1] == 1.0
    assert result.mrr == 1.0

    monkeypatch.setattr(
        eval_core,
        "get_chat_client",
        lambda profile="chat": _FakeAnswerClient(),
    )
    answer = await eval_core.run_answer_probe(
        name="tiny",
        retriever="search_metadata",
        query_id="q1",
        retrieval_limit=2,
        evidence_limit=1,
        timeout_seconds=5,
    )
    assert answer.timed_out is False
    assert answer.evidence_contains_relevant is True
    assert answer.answer_cites_relevant is True
    assert answer.predicted_label == "SUPPORT"
    assert answer.label_correct is True
    assert answer.usage["output_tokens"] == 60

    answer_run = await eval_core.run_answer_eval_dataset(
        name="tiny",
        retriever="search_metadata",
        retrieval_limit=2,
        evidence_limit=1,
        timeout_seconds=5,
    )
    assert answer_run.queries_evaluated == 1
    assert answer_run.timed_out == 0
    assert answer_run.evidence_hit_rate == 1.0
    assert answer_run.answer_citation_hit_rate == 1.0
    assert answer_run.labels_evaluated == 1
    assert answer_run.label_accuracy == 1.0
    assert answer_run.usage["output_tokens"] == 60

    qrels_only = await eval_core.run_answer_eval_dataset(
        name="tiny",
        retriever="search_metadata",
        retrieval_limit=2,
        evidence_limit=1,
        timeout_seconds=5,
        query_limit=1,
        qrels_only=True,
        concurrency=2,
    )
    assert qrels_only.queries_total == 1
    assert qrels_only.queries_evaluated == 1
    assert qrels_only.queries_skipped == 0
    assert qrels_only.concurrency == 2
