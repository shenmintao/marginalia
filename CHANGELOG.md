# Changelog

## Unreleased

## 0.2.7 - 2026-06-19

### Added

- Desktop folder uploads now show an import filter after scanning files and
  folders. Videos are skipped by default, and users can include or exclude
  whole file-type groups or individual extension chips before upload creates
  files or ingest tasks.
- The desktop Library sidebar can now be resized by dragging the separator,
  with the chosen width saved locally.
- Bundled agent skills now include `allowed-tools` / `compatibility` metadata
  and one-shot CLI command references for agents that do not enter the REPL.

### Changed

- `/v1/discover` is now a pure read path by default; seed-scoped relation
  vetting runs only when explicitly requested via `vet=true` / `--vet` and is
  scheduled in the background.
- Task runner settings are refreshed dynamically so runtime configuration
  updates are picked up without restarting long-lived workers.

### Fixed

- Desktop navigation now uses hash routing in Tauri, avoiding full webview
  reloads and repeated cold API calls while moving between Chat and Library.
- The desktop Library now exposes failed-only reprocess actions globally and
  per folder while keeping full-scope reprocess available.
- Empty agent execute responses after planning now surface as explicit errors
  instead of silent zero-token answers.
- Resumed tool results use the expected message roles.
- Duplicate ingest tag attachments are de-duplicated before insert, avoiding
  `entry_tags(entry_id, tag_id)` uniqueness failures.
- Discover relation vetting skips detail queries when there are no candidates.

### Documentation

- Documented PDF and image indexing budgets, chunking behavior, OCR caps,
  embedded PDF image caption limits, standalone image ingest limits, and PDF
  read-time windows in English and Chinese usage docs.

## 0.2.6 - 2026-06-19

### Added

- Desktop bundles now expose `marginalia`, `marginalia-mcp`, and
  `marginalia-worker` CLI wrappers backed by the bundled Python runtime:
  Linux packages install commands under `/usr/bin`, Windows packages include
  `.cmd` wrappers next to the app, and macOS bundles include wrappers under
  `Marginalia.app/Contents/MacOS`.
- `marginalia mcp` now follows CLI backend discovery and exposes structured
  workflow tools for asking Marginalia, upload, download, export, search, and
  metadata reads.

### Changed

- SVG files now route through the text/XML pipeline instead of the raster image
  pipeline, avoiding native rasterization dependencies while preserving
  searchable SVG structure and labels.

### Fixed

- Desktop latest-version checks now allow GitHub release API requests in the
  packaged app CSP, fixing "Failed to fetch" on the About page.

## 0.2.5 - 2026-06-18

### Added

- Desktop Help and About pages, including first-run guidance, settings
  explanations, project links, privacy notes, and a manual latest-version
  check.
- Chinese and English GUI tutorials for non-technical users, linked from both
  README files.
- Settings-page first-run status that explains missing LLM profile
  configuration before users import files or ask questions.
- Upload dialog, Help, and tutorials now remind users to watch Activity or
  Library status until AI file analysis finishes.

### Changed

- Chat now checks required LLM profile configuration before sending a turn and
  surfaces a clearer setup message when model credentials are missing.
- Linux release builds now include AppImage artifacts, and Linux desktop
  bundles are built on Ubuntu 22.04 runners for a lower glibc baseline.

### Fixed

- Ollama OpenAI-compatible profiles now use the legacy `max_tokens` chat
  parameter and avoid unsupported thinking controls during ingest.
- Linux AppImage bundling now sets `NO_STRIP=true` for linuxdeploy and exposes
  all bundled backend shared-library directories during dependency scanning.

## 0.2.4 - 2026-06-11

### Fixed

- Citation footnotes now show the cited quote excerpt while hiding internal
  `quote_status=...` markers; source links and quote/page locators are still
  preserved.

### Changed

- Switched `py7zz` back to the upstream PyPI package at `>=1.3.1`, replacing
  the temporary forked wheel URLs now that upstream publishes ARM64 wheels.

### Release Notes

- Stable release for the 0.2.4 line, including the 0.2.4-rc.1 feature set.

## 0.2.4-rc.1 - 2026-06-10

### Added

- Optional API bearer authentication via `MARGINALIA_API_TOKEN`, with CLI and
  desktop client support for sending the token.
- Auto chat mode now defaults new turns to planner-selected quick/standard/deep
  execution budgets, with visible budget upgrade notices when fresh evidence
  justifies continuing.
- `marginalia eval ablation-run` for candidate-pool component attribution
  across metadata, relation expansion, semantic recall, rerank, and full
  recall configurations.
- `marginalia mcp` / `marginalia-mcp` stdio server exposing the read-only
  retrieval tool set to MCP-capable clients.
- Python linting baseline with `ruff check src tests` in CI.
- Postgres metadata search now uses native text-search expressions with GIN
  indexes, and eval coverage now includes a tiny CJK short-term dataset path.
- Journal recall now annotates stale entry references caused by deletion or
  reprocessing, downgrades stale notes behind current notes, and hides rows
  invalidated by later contradictory reflections.
- `MAINTENANCE_DAILY_TOKEN_BUDGET` can cap rolling 24-hour background
  maintenance LLM usage and defer low-priority speculative tasks when spent.
- Relation discovery now vets directly hit unjudged edges lazily during
  `/discover`; periodic batch `vet_relations` is opt-in via
  `RELATION_BACKGROUND_VETTING_ENABLED`.
- Citation display now marks quote-bearing footnotes as
  `quote_status=verified` or `quote_status=unverified` after checking the
  cited entry's original readable text with whitespace/punctuation
  normalization.

### Changed

- Split the eval implementation into dataset, retrieval, metrics, reporting,
  prompt, and probe modules while keeping `marginalia.eval.core` as the
  compatibility import path.

### Fixed

- `query_sql` now disables DuckDB external access before executing
  model-authored SQL, blocking path-literal, scan-function, and glob-style
  local file reads outside the loaded entries.
- E2E test temp directories are cleaned with a retrying Windows-aware helper.
- Docker compose now binds API and MinIO ports to localhost by default.
- OCR PDF VLM readback no longer counts PDF pages synchronously on the async
  read path.
- Mixed metadata queries keep short CJK terms via LIKE fallback instead of
  silently dropping them from trigram FTS.

### Documentation

- Documented API token use, compose localhost binding, and the known risk of
  syncing a live `MARGINALIA_HOME` with file replication tools.

## 0.2.3 - 2026-06-05

### Added

- CLI chat mode control: `/mode [quick|deep]` now shows or switches the
  investigation mode, and CLI chat requests send the selected mode to
  `/v1/chat`.
- `marginalia init` now includes optional embedding, semantic recall, rerank,
  and evidence-selection settings in the generated starter `.env`.

### Fixed

- Desktop chat restores the latest quick/deep mode when returning to an active
  stream or reopening a historical session.
- Session list and transcript APIs now expose the latest recorded chat mode so
  the UI can replay sessions without silently falling back to deep mode.
- Final-answer continuation and Quick-mode forced-answer guardrails now ask
  the model to keep the same language as the user's latest message.
- `recall_knowledge` now prioritizes selected evidence entries before journal
  note-linked entries when building `candidate_entry_ids`, so rerank/quota
  evidence selection is preserved for follow-up verification and reads.

### Changed

- Clarified internal `search_metadata` naming so local metadata signal ranking
  is not confused with the optional external reranker.
- GitHub release notes now pull the matching version section from
  `CHANGELOG.md`, keeping generated release notes aligned with prior releases.

### Validation

- Added coverage for CLI quick/deep mode requests, starter `.env` retrieval
  settings, session mode restore, and selected-evidence candidate ordering.
- Main CI passed for the post-0.2.2 fixes before preparing this release.

## 0.2.2 - 2026-06-04

### Added

- Settings UI and API controls for embedding, semantic recall, rerank, and
  evidence-selection configuration.

### Fixed

- Citation footnotes now hide raw `entry_id`, `quote`, and `reason` metadata
  in more model output variants, including quoted `entry_id` values and fields
  emitted in a different order.
- OpenAI-compatible chat adapters now convert DeepSeek-style DSML text tool
  calls into real tool calls instead of leaking pseudo-XML into the answer.
- Quick mode now performs a forced final-answer retry when the capped final
  turn still tries to call a tool, reducing "no final answer" failures.

## 0.2.1 - 2026-06-03

### Added

- Chat UI **Quick / Deep** mode switch.
- Request-level chat mode API: `POST /v1/chat/{session_id}` now accepts
  `mode: "quick" | "deep"`.
- Deterministic, non-LLM `read_files` result compression for long Agent reads.
  Large text, PDF text, JSON, log, and code-like results can now be trimmed
  before entering the chat model while preserving page/line/offset reopen
  anchors.
- `read_files` now accepts `compress: false` for exact reopen reads of omitted
  ranges.
- Runtime settings for read result compression, including a Settings-page
  toggle and `.env` defaults via `READ_COMPRESSION_*`.
- Broader text-pipeline routing for common code/config/data extensions such as
  `.json`, `.yaml`, `.toml`, `.xml`, `.html`, `.csv`, `.py`, `.js`, `.ts`,
  `.go`, `.rs`, `.java`, `.sql`, and shell scripts.

### Changed

- Quick mode keeps the plan phase but caps execute to three LLM calls: the
  first two may gather evidence with tools, while the third disables tools and
  must answer from collected evidence. Deep mode keeps the existing full ReAct
  investigation budget.
- Documentation now describes the quick lookup path separately from the full
  deep investigation workflow.
- Agent instructions now treat compressed `read_files` output as lossy:
  visible text remains quoteable, but omitted markers must be reopened before
  quoting or relying on omitted evidence.

### Validation

- Added unit coverage for PDF page, text, JSON, log, and code read compression.
- Added `read_files` e2e coverage for compressed reads and `compress: false`
  reopen behavior.

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
