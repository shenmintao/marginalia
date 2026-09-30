# Changelog

## Unreleased

## 0.3.8 - 2026-09-30

### Fixed

- Linux AppImage releases now ship `.DirIcon`, the root PNG icon, and the desktop
  entry as regular files, avoiding links to build-machine paths. The release
  pipeline checks these files in the packaged image before upload.
- AppImage release filenames now use `x86_64` and `aarch64` architecture names
  without the redundant `linux` component, following the catalog's naming advice.
- AppImage files are readable by all users and existing executable files retain
  execute permission for all users, including Tauri's wrapped launcher.

## 0.3.7 - 2026-09-01

### Changed

- LLM profiles now expose only provider, endpoint, model, context window, and
  genuine model capabilities. The internal model registry resolves compatible
  request adapters, token counters, reasoning controls, and output-token field
  names automatically.

## 0.3.6 - 2026-08-20

### Changed

- Chat delivery is now durable: every public event is committed before SSE
  delivery, frames carry resumable cursors, desktop and CLI clients reconnect
  automatically, and explicit cancellation is independent from viewer
  disconnects.
- Tool calls expose replay-stable turn/index identifiers without leaking or
  replacing provider-side identifiers used for model tool-result pairing.
- Session and completed-task collection APIs support stable keyset pagination,
  backed by composite indexes that also keep bounded retention scans fast.
- Optional document, storage-byte, ingest-backlog, and concurrent-chat gates
  reject excess work with HTTP 429 before expensive processing begins.
- Relation mining, section embeddings, task cleanup, and event cleanup now
  have explicit read, candidate, page, batch, and retention bounds.
- PostgreSQL can run through transaction-pooled proxies with disabled asyncpg
  statement caching and globally unique prepared-statement names.
- Managed deployments can migrate once with `marginalia-db-prepare`, disable
  runtime schema DDL on API/worker replicas, use `/live` for liveness and
  `/ready` for bounded database/storage readiness checks, and run queue-only
  workers without periodic scheduling.
- Prompt-cache reporting distinguishes whole-prompt coverage from eligible
  prefix reuse, and finalization keeps cache-affecting request parameters
  stable across execution rounds.

### Fixed

- Compatible-provider tool arguments receive bounded, semantics-safe JSON
  repair; ambiguous malformed calls are rejected and can be corrected up to
  two times instead of executing with invented values.
- Durable event replay waits for the persisted terminal event when conversation
  completion and event commits race, avoiding prematurely closed streams.
- Legacy CLI SSE streams without durable conversation identities still finish
  cleanly, while identified turns continue reconnecting until a terminal event.
- Desktop backend overrides are now verified against Marginalia's `/health`
  response before they are saved. Ollama and LM Studio model endpoints can no
  longer be mistaken for the GUI backend, and a bad existing override can be
  cleared from the startup screen to restore the bundled backend.
- The Settings page no longer exposes or persists the bundled sidecar's
  ephemeral runtime port as a user-configured remote backend.

## 0.3.5 - 2026-08-19

### Changed

- Agent execution now has an explicit evidence-gathering checkpoint before
  final composition. Once research is closed, tools are disabled and the
  model receives up to two bounded attempts to return a complete answer.
- Source citations selected at the checkpoint are validated against successful
  `read_files` output. Entry IDs, exact visible quotes, and PDF page or PPTX
  slide positions must all be supported by evidence from the current turn.

### Fixed

- Final answers no longer lose article-source anchors when the model emits body
  markers without their definitions. The runtime assigns stable markers and
  appends verified definitions deterministically before display-link rewriting.
- Model-written definitions for assigned markers are replaced with the
  validated manifest, and missing body markers receive a compact source
  fallback so every verified citation remains reachable.

## 0.3.4 - 2026-08-19

### Changed

- Long text and Office documents now produce stable, named sections; oversized
  inputs are indexed in bounded concurrent chunks with file-level summaries,
  coverage metadata, and deterministic heuristic fallbacks.
- Citation links now retain complete source locators: PDF page plus verified
  quote, DOCX block plus quote, PPTX slide plus quote, and XLSX sheet plus
  cell or row plus quote. The desktop viewer preserves and consumes every
  locator field together.
- Agent replay now validates stored tool history strictly, keeps provider
  prompt prefixes append-only, applies per-tool timeouts, canonicalizes tool
  schemas, and reports cache-eligible hit/reuse metrics in live and replay APIs.
- Agent tool fan-out now uses a bounded rolling pool, configurable with
  `AGENT_MAX_PARALLEL_TOOL_CALLS`, so one model response cannot open an
  unbounded number of database sessions or retain every tool result at once.
- Scanned-PDF visual question answering caps each read to five pages, sends at
  most three page images per provider request, enforces serialized payload and
  render-size budgets, and falls back to single-page calls when a model does
  not support multiple images.
- PDF, DOCX, and PPTX question reads now preserve readable source text before
  considering OCR or document vision. Visual inspection is reserved for
  requested ranges without readable text, while image reads fall back to their
  persisted descriptions when a live vision call fails.
- Embedding and rerank HTTP connections are reused within each event loop.
  Provider responses are validated for vector count/dimensions and usable
  rerank results before they enter the semantic index or ranking pipeline.
- Ingest task outcomes now report extraction, vision, intelligence, embedding,
  status-persistence, and total-pipeline stage durations. Throughput reporting
  distinguishes scheduled reprocessing from ordinary uploads without changing
  the underlying task kind.
- The provider diagnostics action now sends a real image to the vision profile
  and validates enabled embedding and rerank providers as well as chat models.
- Retrieval evaluation now includes a concurrent load runner with throughput,
  latency percentiles, quality metrics, and enforceable thresholds.
- Default task-worker and ingest-LLM concurrency are both four, while remaining
  independently configurable for workload and provider limits.
- LLM profiles now carry explicit dialect, context-window, tokenizer, vision,
  tool, temperature, and output-token-parameter capabilities. Token-aware
  request compaction keeps conversation history within the resolved model
  window without modifying stored turns.
- Prompt-cache metrics now include a configurable three-state SLO verdict:
  met, breached, or insufficient data.
- Whole-library semantic rebuilds page through database entries, and confident
  scoped section matches backfill locators for lexical recall candidates.
- Task deliveries now carry a unique owner token. Heartbeats, completion,
  retry, and stale-lease recovery use owner-and-lease compare-and-swap checks;
  loss of ownership cancels the old handler, and periodic ticks use time-slot
  dedup keys so every completed tick leaves a distinct successor.
- Worker retries use configurable bounded exponential backoff. Database
  bootstrap also collapses legacy duplicate active tasks before installing the
  active-dedup constraint, preserving the most executable delivery.
- Upload limits are enforced while multipart bytes stream through ASGI, before
  framework spooling. File bytes are counted exactly, non-file multipart data
  is bounded separately, and an obviously oversized `Content-Length` is
  rejected without consuming the body.
- Upload commit ambiguity is compensated safely: local partial files are
  removed, bounded S3 multipart writes abort on failure, object deletions are
  persisted as retryable tasks, and soft-deleted database rows are never
  reused as live content-addressed uploads.
- A duplicate upload now resumes failed ingest or schedules a per-file semantic
  refresh for ready content. Refreshes reuse vectors only when provider,
  model, dimensions, and section text hash all match the current index.
- PostgreSQL deployments serialize conflicting tool scopes and concurrent
  turns for one session with transaction advisory locks, while retaining the
  lightweight in-process locks used by SQLite.
- Late-page PDF visual reads render only the requested page range; image size,
  page count, serialized request size, and multi-image compatibility remain
  bounded independently.

### Fixed

- Settings connection probes use a small but provider-compatible output
  budget instead of a one-token cap rejected by some reasoning models.
- Answer-language instructions now stay anchored to the current user's
  original question even when retrieved evidence or runtime messages use a
  different language.
- Semantic indexing ignores generated placeholder-only section titles while
  preserving real OCR and text headings.
- Legacy databases with duplicate active task dedup keys upgrade without
  manual repair, and configurable retry delays remain capped even after many
  attempts.
- PPTX reads no longer discard earlier slide text merely because a later slide
  is empty, and empty vision-provider responses are surfaced as explicit
  errors instead of synthetic answer text.

## 0.3.3 - 2026-07-03

### Added

- Multimodal chat input: paste or drag images into the chat composer to ask
  about your library together with a picture. Images ride the current turn
  only (never re-sent in history, so token cost stays flat) and render in the
  transcript. `MARGINALIA_CHAT_VISION` (auto|on|off, default auto) probes the
  chat model once per model and, for a text-only model, routes images through
  the `vision` profile as an injected description — automating the manual
  "describe the image first" workaround. Per-turn caps via
  `MARGINALIA_CHAT_IMAGE_MAX_COUNT` / `MARGINALIA_CHAT_IMAGE_MAX_BYTES`. Pasted
  images are persisted per turn and re-displayed as thumbnails when a session's
  transcript is reloaded (UI only — still never re-sent to the model).
- `POST /v1/settings/llm/test` probes each configured LLM profile with a tiny
  chat call (bounded by a timeout) so a mistyped key/base-URL/model is caught
  at config time; a "Test connection" button surfaces per-profile status. A
  settings PUT that first makes required profiles valid now auto-reprocesses
  ingests that failed before a key existed.
- `OCR_MAX_PAGES` (default 300) caps scanned-PDF OCR and records an
  `ocr_page_cap` partial-coverage reason when it trips.

### Changed

- Transient provider failures (rate limits, 5xx/529 overload, timeouts) are
  retried with bounded exponential backoff honoring `Retry-After`, so a brief
  overload no longer discards a whole agent turn's accumulated tool work.
- CPU-bound document parsing (PDF/DOCX/PPTX/spreadsheet) runs off the event
  loop, keeping the API responsive and worker heartbeats alive during large
  ingests.
- GUI search tokenizes multi-word queries and ranks results instead of
  matching one contiguous phrase; the per-hit related-entries walk is limited
  to the top hits so latency no longer scales with match count.
- Release artifacts (Docker image, desktop sidecar) install from the locked
  requirements exported from `uv.lock`, so shipped versions match what CI
  tested; CI gained a `uv.lock` drift gate and a Docker build check.

### Fixed

- Selective WebDAV publish no longer leaks the full folder/tag taxonomy or any
  sessions/conversations/journals — only the taxonomy and relations reachable
  from the selected entries ride along.
- Agent per-call token budgets are sized for reasoning models (plan 2048,
  execute 4096, vision-describe 4096), which spend most of their output budget
  on hidden reasoning before any visible text — the old smaller caps were
  consumed by reasoning and truncated the plan/answer/image description to
  empty ("can't read the image" even when the model and image were fine).
- The LLM test-connection probe treats a rate-limit (429) as reachable and no
  longer retry-storms it into a false timeout when several profiles share one
  provider account.

## 0.3.2 - 2026-07-03

Hardening release from a full code audit: fixes for data-loss, correctness,
and safety defects across WebDAV sync, the mirror vault, ingest pipelines,
semantic recall, the agent runtime, the CLI/MCP surfaces, and the desktop app.

### Fixed

- WebDAV pull no longer clobbers newer local edits, resurrects locally
  deleted entries, or wipes local-only tag assignments: a minimal conflict
  guard skips rows whose local `updated_at` is newer, preserves newer local
  deletions, and merges tags instead of replacing them.
- WebDAV pull re-downloads remotely-changed file content instead of marking
  the stale local bytes as hydrated, so `files.sha256` no longer diverges
  from the stored blob.
- WebDAV folder/catalog import handles children exported before their parent
  (no foreign-key crash), reconciles same-name folders across machines by
  `(parent_id, name)`, and rejects path-shaped remote ids/names.
- WebDAV publish records and checks a `library_id`, refusing to overwrite an
  unrelated remote library, and a full publish now reads-and-merges the remote
  snapshot instead of dropping remote-only entries.
- Mirror uploads into a folder (GUI `folder_id` style) now write the file into
  the folder's directory instead of the vault root, and disk/DB name-collision
  suffixes agree.
- Folder rename/move relocates the on-disk mirror directory; renaming or moving
  a not-yet-hydrated WebDAV entry no longer 500s; and folder relocation is
  crash/partial-failure tolerant and runs off the event loop.
- `scan`/`apply` correctly handles moves to the vault root and no longer
  mis-attributes a deleted duplicate's file to another entry.
- Ingest is more robust: archives containing dangling/absolute symlinks or an
  inner tar in a subdirectory no longer crash; decompression bombs are refused
  from the declared sizes before extraction; and text files in cp1252/latin-1
  and similar legacy encodings decode correctly instead of as UTF-16 mojibake.
- Semantic index refreshes are serialized within and across processes, use
  unique temp files, cap embedded text length, avoid wiping a populated index
  on an empty entry set, avoid re-embedding the whole library on the first
  per-file refresh, and recover from a stale sqlite-vec sidecar.
- Agent runtime: bounded resume-history replay, a terminal branch for
  filtered/refused responses (no more burning the round budget), off-loop PDF
  quote location, escaped citation link text, and DSML text tool-call parsing
  gated to the providers that actually use it.
- Metadata search rescues short non-CJK terms (`AI`, `Go`), escapes `%`/`_`
  wildcards, and keeps short CJK terms in ranking; Postgres CJK search routes
  through ILIKE.
- CLI/MCP: SSE indentation is preserved in streamed answers; the MCP stdio
  server dispatches requests concurrently, ignores unknown notifications, and
  drains in-flight work on EOF; `/ls` lists entries; Ctrl-C during a chat turn
  cancels the turn instead of quitting; errors surface when the spinner is
  disabled; and `~` is expanded in upload paths.
- Security hardening: `LocalStorage` refuses path-escaping keys; LLM-supplied
  regexes in `query_log`/`analyze_container` run in a killable subprocess with
  a wall-clock timeout; folder-download zip members are sanitized against
  zip-slip; WebDAV routes no longer echo raw internal errors; and the server
  logs a warning when bound to a non-loopback host without a token.
- Desktop: token-protected backends load PDFs/images/EPUB/Office/downloads via
  authenticated blob URLs; external links and the Office print button work
  under Tauri; viewer toolbars and errors are localized; the "quote not found"
  banner is styled; a stale search-error banner clears; and a port-conflict on
  the fixed backend port surfaces an actionable error screen instead of an
  infinite spinner (attaching to an already-running backend still works).
- Alembic `upgrade head` succeeds on PostgreSQL when revision ids exceed 32
  characters; interrupted SQLite table rebuilds are recovered on next start;
  and the pooled connection is no longer left with foreign keys disabled.

### Added

- `MARGINALIA_UPLOAD_MAX_BYTES` caps `POST /v1/upload` (default `0` =
  unlimited); oversized uploads are rejected with 413 before the body is
  spooled to disk.

### Changed

- `glowpy` is pinned to an exact commit instead of a moving branch.
- The unused `cost_estimate` / `total_cost_estimate` fields now surface as
  `null` rather than a misleading constant `0`.

## 0.3.1 - 2026-07-02

### Added

- Added provider call TPS limiting for LLM, embedding, and rerank requests,
  with runtime settings overlay support.
- DOCX and PPTX embedded images can now be described by the vision profile,
  queried with `read_files(question=...)`, and persisted for fallback reads.

### Changed

- PDF, PPTX, and DOCX image descriptions are inserted back into native document
  positions before indexing: PDF figures per page, PPTX images per slide, and
  DOCX images near their source block.
- Embedding request batch size is capped at 10 for desktop and backend
  settings.

### Fixed

- Text-layer PDF readback now includes persisted figure descriptions for
  targeted page reads and pattern searches.
- Existing invalid overlay values such as `embedding_batch_size > 10` are
  ignored instead of overriding safe defaults.

## 0.3.0 - 2026-07-01

### Added

- WebDAV knowledge-pack sync can publish and consume snapshots without
  syncing the live `MARGINALIA_HOME` directory.
- Desktop Library now provides separate WebDAV upload and download sync
  flows that list changed files and let users choose which entries to sync.
- Desktop Settings can save WebDAV connection details and sync lightweight
  remote status, showing the remote snapshot time and id.

### Fixed

- Follow-up chat turns now give the planner lightweight same-session context,
  so terse requests like "continue" or "expand that" stay on the prior topic
  instead of being mistaken for standalone small talk.
- EPUB citation links now carry quote locators and the desktop EPUB viewer
  searches the spine to jump to the cited passage.
- WebDAV JSONL metadata parsing now preserves Unicode line separators inside
  JSON strings and reports the affected metadata file and line on parse errors.
- WebDAV download sync now reuses existing local tags with the same name and
  facet, including case-only variants such as `FAQ`/`faq`, avoiding
  `tags(name, facet)` uniqueness failures when importing a remote snapshot.
- WebDAV metadata import now writes `summarized_journal_ids` as SQL NULL for
  `reflect_turn` journal rows, avoiding the journal integrity check failure
  exposed by existing remote snapshots.
- The WebDAV download sync dialog now uses a download icon instead of matching
  the upload sync icon.

## 0.2.11 - 2026-06-30

### Fixed

- Desktop backend discovery now verifies `/health` instead of only checking
  whether a local port is open, avoiding false reuse of unrelated local
  services.
- Packaged desktop builds now write early launcher diagnostics to
  `<MARGINALIA_HOME>/logs/launcher.log`, including skipped backend launches,
  missing bundled backend resources, and sidecar spawn failures.
- Packaged desktop builds now write frontend diagnostics to
  `<MARGINALIA_HOME>/logs/frontend.log`, including backend URL resolution,
  health-check delays, network failures, and uncaught frontend errors.
- Backend logs now include startup milestones, request failures, slow
  requests, upload diagnostics, and task runner lifecycle events.

## 0.2.10 - 2026-06-26

### Fixed

- `read_files` deep reads now reopen original files or complete extracted
  text instead of ingest/index previews, with consistent heading, page, line,
  pattern, and offset behavior across EPUB, PDF, Office, email, and archive
  members.
- Desktop file viewers are split by format (PDF, Office, EPUB, email, image,
  archive) while keeping the Library toolbar entry point stable, reducing
  regressions between unrelated preview types.
- Desktop Office previews now allow the `@silurus/ooxml` WebAssembly and
  worker sources required by the Tauri/WebView2 runtime, avoiding a stuck
  loading state for DOCX/PPTX/XLSX files.
- Office previews now surface a timeout error if the embedded viewer never
  finishes initializing.
- Added regression coverage for continuing a loaded historical session with
  prior turns replayed into the execute phase.

## 0.2.9 - 2026-06-25

### Fixed

- EPUB previews now open API-served original files reliably and expose
  Office-style current-page / total-page navigation.
- SQLite startup bootstrap now runs post-baseline shims in separate
  transactions, allowing existing user libraries with live `file_entries`
  foreign keys to migrate the expanded file-kind check constraint.

## 0.2.8 - 2026-06-24

### Added

- Release builds now include a macOS Intel (x86_64) DMG alongside the Apple
  Silicon DMG.

### Changed

- Headroom-based read compression is now the standard path for long text,
  logs, archive members, PDFs, and read-files tool output.
- The Headroom compression core is vendored so packaged builds no longer
  depend on the external Headroom package or its optional ONNX stack.
- Desktop settings and help copy now use the current compression setting names.

### Fixed

- Session replay handles stopped/error turns more reliably.
- PDF inline content responses now support byte ranges for better large-file
  viewer performance.

## 0.2.7 - 2026-06-19

### Added

- Desktop folder uploads now show an import filter after scanning files and
  folders. Videos are skipped by default, and users can include or exclude
  whole file-type groups, individual extensions, or individual files in a
  downloader-style selection table before upload creates files or ingest tasks.
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
- Closed chat sessions can be reopened by sending another message, so users can
  continue the same conversation after restarting the app or computer.
- Interrupted or overlong chat turns are finalized server-side and replay as
  stopped/error turns instead of leaving the desktop transcript spinning
  forever.
- Resumed tool results use the expected message roles.
- Duplicate ingest tag attachments are de-duplicated before insert, avoiding
  `entry_tags(entry_id, tag_id)` uniqueness failures.
- Files are now marked `failed` whenever their `ingest_file` task reaches
  terminal `dead`, including stale-task recovery and no-LLM startup sweeps.
  A bootstrap repair also reconciles older databases where files were left in
  `processing` after dead ingest tasks.
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
