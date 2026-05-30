# Changelog

## 0.2.0 - 2026-05-30

Marginalia 0.2.0 moves the project toward a personal-library research agent:
retrieval remains local-first and source-grounded, while optional semantic
recall, reranking, and evaluation commands make report-generation quality
measurable.

### Added

- Optional semantic recall using OpenAI-compatible embeddings, with
  DashScope/Bailian `text-embedding-v4` as the documented default.
- Optional `sqlite-vec` semantic-index backend, with file-index fallback.
- Optional second-stage reranking with separate `RERANK_*` credentials.
- Hybrid `recall_knowledge` evaluation support with batched recall, answer
  probes, answer-run aggregates, and report comparison.
- `marginalia eval compare-report`, which compares one-shot RAG reports with
  the full ReAct investigation workflow using blind pairwise judging.
- BEIR-style dataset import that runs ingest synchronously and supports
  resumed/concurrent imports.
- Entry metadata FTS expansion for richer lexical recall.

### Changed

- Semantic recall and rerank are opt-in; no chat, vision, or ingest API key is
  reused implicitly for embedding or reranking.
- `recall_knowledge` can merge lexical and semantic candidates, apply RRF-style
  scoring, optionally rerank, and select evidence with source quotas.
- Evaluation reports distinguish candidate-pool retrieval metrics from
  final-answer/report metrics.

### Validation

- SciFact 300 retrieval with rerank top-80 reached MRR 0.7226, hit@10 0.8800,
  and hit@100 0.9133 in local validation.
- SciFact 300 bounded answer-run with rerank top-80 and quota reached evidence
  hit 0.8667, citation hit 0.7133, and label accuracy 0.8085.
- A 30-query end-to-end report comparison favored the ReAct workflow over
  one-shot RAG in 26/30 cases, with 2 one-shot RAG wins, 2 ties, and 1 timeout.

### Notes

- ReAct report generation improves report quality at substantially higher
  latency and token cost. It is best treated as a deep investigation mode, not
  as the default path for every quick lookup.
- Some OpenAI-compatible models may occasionally emit invalid JSON tool
  arguments; the runtime tolerates these failures, but they can waste tool
  turns and should be improved in later releases.
