# Marginalia

> Chinese README: [README.zh-CN.md](README.zh-CN.md)
> Detailed design: [DESIGN.md](DESIGN.md)
> GUI setup guide: [English](docs/GUI_TUTORIAL.md) · [中文](docs/GUI_TUTORIAL.zh-CN.md)

**Turn your PDFs, notes, spreadsheets, logs, and archives into a private AI
library that answers from original sources.**

Marginalia is a local-first research agent for people with messy private
knowledge bases. It keeps your files in a normal folder tree, builds useful
library metadata around them, and makes the agent read the relevant original
file windows before it writes a cited answer.

[Download desktop app](https://github.com/shenmintao/marginalia/releases) ·
[GUI setup guide](docs/GUI_TUTORIAL.md) · [CLI quickstart](#cli-quickstart) · [Usage guide](USAGE.md) ·
[Design notes](DESIGN.md)

![Marginalia promotional hero](docs/images/marginalia-promo-en.png)

![Marginalia desktop app screenshot](docs/images/desktop-screenshot-en.jpg)

## Why Use It

- You have research papers, meeting notes, PDFs, tables, logs, screenshots, and
  archives that do not fit cleanly into one app.
- You want answers that cite the source material instead of a black-box vector
  search layer over chunks.
- You need both quick lookups and slower investigation-style reports over the
  same private library.
- You want local-first storage: the default `mirror` backend keeps your library
  as readable files under `MARGINALIA_HOME/library`.

## What It Does

- Ingests text, Markdown, PDFs, DOCX, images, spreadsheets, logs, and archives.
- Organizes material with folders, catalogs, tags, views, metadata, journals,
  and relation mining.
- Recalls candidates with lexical search by default, plus optional embeddings,
  `sqlite-vec`, reranking, and source quotas.
- Reads original sections, pages, lines, archive members, or table slices before
  answering.
- Produces cited answers and reports, then writes durable investigation notes
  that future turns can recall.

## Try It

### Desktop App

Download the latest desktop package from
[GitHub Releases](https://github.com/shenmintao/marginalia/releases):

- **Windows**: x64/arm64 installer and portable zip.
- **macOS**: Apple Silicon DMG.
- **Linux**: x64/arm64 `.deb` and `.rpm`.

The desktop builds bundle their own Python runtime. They are currently unsigned,
so Windows SmartScreen or macOS Gatekeeper may ask you to confirm the first
launch.

Desktop bundles also include CLI wrappers backed by the bundled Python
runtime. They share the same `MARGINALIA_HOME` as the desktop app, so the CLI,
MCP server, reusable backend, and worker work without installing a separate
system Python package.

- **Linux `.deb` / `.rpm`**: installs `marginalia`, `marginalia-mcp`, and
  `marginalia-worker` under `/usr/bin`.
- **Windows installer / portable zip**: includes `marginalia.cmd`,
  `marginalia-mcp.cmd`, and `marginalia-worker.cmd` next to
  `Marginalia.exe`. Use full paths in MCP clients or add the install folder to
  `PATH`.
- **macOS DMG**: includes wrappers inside the app bundle:
  `/Applications/Marginalia.app/Contents/MacOS/marginalia`,
  `marginalia-mcp`, and `marginalia-worker`.

- **Windows**: click **More info** -> **Run anyway** if SmartScreen blocks the
  first launch.
- **macOS**: after dragging the app to `/Applications`, run
  `xattr -dr com.apple.quarantine /Applications/Marginalia.app` if Gatekeeper
  reports that the app is damaged or cannot be verified.

### CLI Quickstart

Requires Python 3.11+.

```bash
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
marginalia init
```

Edit `.env`:

```ini
MARGINALIA_API_HOST=127.0.0.1
MARGINALIA_API_PORT=8000
LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
```

Run the embedded CLI + API + worker:

```bash
marginalia
```

Then:

```text
marginalia> /upload ./paper.pdf /papers/
marginalia> /background
marginalia> compare this paper with my Paxos notes
marginalia> /export
```

The first launch bootstraps the database schema automatically.

To share one backend across the desktop app, CLI sessions, MCP, skill-driven
automation, or external HTTP clients, start the reusable HTTP backend instead:

```bash
marginalia serve
```

`marginalia serve` reads `MARGINALIA_API_HOST` and `MARGINALIA_API_PORT` from
`.env` and writes its live URL to `MARGINALIA_HOME/runtime/server.json`.
Desktop and CLI clients auto-discover that file; skills inherit this when they
drive the `marginalia` CLI. Explicit `--server URL` or `MARGINALIA_SERVER`
still take precedence.

## Example Questions

```text
Compare this Raft paper with my Paxos notes.
Find the incident timeline across the logs and the postmortem.
Which uploaded papers support this claim, and which contradict it?
Summarize the spreadsheet, then cite the rows used for the conclusion.
Turn this folder into a cited research brief.
```

## How It Differs From Plain RAG

Marginalia is not just "retrieve top-k chunks and answer." The agent can recall
prior investigations, inspect structured metadata, follow related entries, read
original source windows, and correct its search path before writing. Quick mode
keeps this bounded for short lookups; Deep mode keeps the full ReAct
investigation loop when coverage matters more than latency.

## The Retrieval Funnel

```text
user question
  -> plan
  -> recall_knowledge            # journal + metadata + optional semantic recall
  -> search_metadata/list_folder # focused follow-up over names, summaries, tags
  -> read_entries_metadata       # sections, extra, related entries
  -> discover/related entries    # graph-based neighbours
  -> read_files                  # original text/page/line/member/table slice
  -> answer with footnotes
  -> reflect_turn                # durable journal memory
```

The agent is instructed to use `recall_knowledge` for broad material location.
That tool resolves tag hints, searches prior journal notes and entry metadata,
optionally adds semantic candidates, ranks the merged pool, and returns compact
candidate IDs for batched metadata verification and source reads. Lower-level
tools such as `search_journal`, `search_metadata`, and `materialize_view`
remain available for focused follow-up and debugging.

Metadata text search is indexed in both supported database modes. SQLite uses
the local FTS5 trigram table; Postgres uses native `to_tsvector` /
`websearch_to_tsquery` expression GIN indexes over file and entry metadata.
Chinese short terms that are too small for trigram tokenization are preserved
with a bounded LIKE fallback in mixed metadata queries.
Journal recall also validates referenced entries at read time. If a prior
note points at a deleted entry or a file reprocessed after the note was
written, the note is kept for audit but marked stale and ranked behind current
notes. Later reflections can also mark directly contradicted journal rows
`invalidated_*`; active recall hides them by default while audit queries can
include them.

## Supported Ingest Pipelines

- `text`: text, Markdown, reStructuredText, code-like text.
- `pdf`: text-layer PDF, long-PDF page windows, PDF page labels, scanned-PDF OCR fallback when a vision profile is configured.
- `image`: image indexing and description when a vision profile is configured.
- `docx`: Word documents.
- `spreadsheet`: CSV, TSV, JSON, XLSX, Parquet and related table formats.
- `log`: logs and logrotate variants.
- `archive`: zip, tar, 7z, rar, gz, bz2, xz, iso, cab and other py7zz-supported containers.

## Retrieval Evaluation

External retrieval datasets can be imported from a local BEIR-style directory:

```text
<dataset>/
  corpus.jsonl
  queries.jsonl
  qrels/test.tsv
```

Import is synchronous. Each corpus document is written as a normal entry and
immediately passed through the ingest pipeline, so the command returns only
after the eval corpus is indexed.

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval import-beir scifact ./datasets/scifact
MARGINALIA_HOME=./runtime/eval/scifact EMBEDDING_API_KEY=... marginalia eval build-semantic-index scifact
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever search_metadata --k 10,50,100 --json report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever semantic_recall --k 10,50,100
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval ablation-run scifact --k 10,50,100 --json ablation-report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval answer scifact --retriever recall_knowledge --query-id <qid> --timeout-seconds 300
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval answer-run scifact --retriever recall_knowledge --qrels-only --query-limit 20 --concurrency 10 --json answer-report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval compare-report scifact --query-limit 30 --concurrency 3 --json compare-report.json
```

Use a dedicated `MARGINALIA_HOME` for external benchmarks unless you
intentionally want benchmark documents inside your personal library.
`eval build-semantic-index` uses the configured embedding provider. The
default is Alibaba Cloud Model Studio / DashScope `text-embedding-v4`; set
`EMBEDDING_API_KEY` before building. Embedding credentials are intentionally
separate from `LLM_*` profiles. Semantic recall is optional and disabled by
default; set `SEMANTIC_RECALL_ENABLED=true` to merge semantic candidates from
the default semantic index with the lexical metadata recall path. The eval CLI
index builder targets imported datasets; the GUI/API can enqueue a whole-library
semantic-index rebuild for the default index after embedding model or dimension
changes. Ingest also refreshes the affected file's semantic vectors after a
successful run when semantic recall is configured. If the optional `sqlite-vec`
dependency is installed, the semantic index also writes `vectors.sqlite` and
search uses it before falling back to the file index. Install with
`pip install -e ".[semantic]"`, or set `SEMANTIC_INDEX_BACKEND=file` to keep
only the file backend.
Optional reranking can refine the merged candidate pool before evidence
selection. Enable it with `RERANK_ENABLED=true`, `RERANK_API_KEY=...`, and
optionally `RERANK_MODEL=qwen3-rerank`. Rerank credentials are also separate
from `LLM_*`; no chat or vision key is reused implicitly. Evidence selection
defaults to `EVIDENCE_SELECTION=quota`; set `EVIDENCE_SELECTION=rerank` to take
the reranked top evidence directly.
The eval report treats `hit@k` and `candidate_recall@k` as the investigation
candidate-pool metrics; MRR and nDCG are ranking-efficiency diagnostics.
`eval ablation-run` runs the candidate-pool matrix for metadata-only,
metadata-plus-relations, hybrid semantic recall, hybrid-plus-relations,
hybrid-plus-rerank, and full recall. It reports deltas against metadata-only
so relation expansion, semantic recall, and rerank contributions can be
tracked before changing the agent loop.
`eval answer` is a bounded final-answer probe: it retrieves candidates, reads
limited source text, performs one answer-generation call, and reports whether
the answer cited a qrels-relevant document. `eval answer-run` repeats the same
bounded probe across imported queries and reports aggregate final-answer
citation hit rate; use `--qrels-only` to apply `--query-limit` after filtering
to imported qrels-backed queries and `--concurrency` to run independent answer
probes in parallel. When BEIR query metadata includes SciFact-style
SUPPORT/CONTRADICT labels, the answer report also includes label accuracy.
`eval compare-report` runs a blind end-to-end comparison between a one-shot
RAG report and the full ReAct investigation workflow on the same query set.
When SciFact-style gold labels are available, the judge prioritizes verdict
correctness before report completeness.

Latest local validation on SciFact 300:

- Retrieval with `recall_knowledge` + rerank top-80 reached MRR 0.7226,
  hit@10 0.8800, and hit@100 0.9133.
- Bounded final-answer probes with rerank top-80 and quota evidence selection
  reached evidence hit 0.8667, citation hit 0.7133, and label accuracy 0.8085.
- A 30-query end-to-end report comparison favored the full ReAct workflow over
  one-shot RAG in 26/30 cases, with 2 one-shot RAG wins, 2 ties, and 1 timeout.

These results support Marginalia's current positioning: for quick lookups it
behaves like a hybrid RAG system, while the full ReAct workflow is a slower
deep-investigation path that can produce better source-grounded reports.
They should not be read as a claim of general benchmark SOTA: the dataset is
small, the comparison target is a local one-shot RAG baseline, and final
quality still depends on model behavior, ingest quality, and available
evidence.

## CLI Surface

`marginalia` with no arguments opens the interactive REPL. The same command
surface is also available as one-shot subcommands for scripts, CI, and agents
that do not use MCP:

```bash
marginalia ask "Compare this Raft paper with my Paxos notes"
marginalia search "raft consensus" --json
marginalia info <entry_id> --json
marginalia discover <entry_id> --top-k 12 --json
marginalia check --json
marginalia ingest --all --yes --json
marginalia reprocess failed --json
```

One-shot commands use the same backend discovery model as the REPL: explicit
`--server URL`, then `MARGINALIA_SERVER`, then
`MARGINALIA_HOME/runtime/server.json`, and finally an embedded backend. Text
output is meant for humans; `--json` keeps stdout structured for automation.

Slash commands:

```text
/help                         list commands
/upload <local> <remote>      upload a file or directory into the vault
/check                        diff mirror vault vs database
/ingest <path> | --all        sync manual vault edits into the database
/reprocess failed             re-run ingest for failed files
/reprocess folder <id> failed re-run failed files in one folder subtree
/search <query>               metadata recall
/info <entry_id>              entry metadata and preview
/discover <entry_id> [N]      related entries from the evidence graph
/discover <entry_id> --all    include unvetted relation signals
/discover <entry_id> --vet    queue background vetting for direct signals
/tree                         folder tree
/download <id> [dest]         download file or folder zip
/export [conversation_id]     export answer and citations
/tend                         run a maintenance pass
/background                   show queued/running tasks
/mode [auto|quick|deep]       show or change chat mode
/new / /clear / /quit         session control
```

Any non-slash input is sent to the investigator agent. Chat defaults to
`auto`: the planner selects a quick/standard/deep execution budget from a
plain `BUDGET:` control line and the runtime can upgrade it while tools are
still producing new evidence. `/mode quick` and `/mode deep` remain manual
overrides.

## MCP Server

Marginalia can also run as a stdio MCP server for external agents:

```bash
marginalia mcp
# or
marginalia-mcp
```

The MCP server uses the same backend discovery model as the CLI: explicit
`--server URL`, then `MARGINALIA_SERVER`, then
`MARGINALIA_HOME/runtime/server.json`, and finally an embedded backend if
nothing is already running. A Claude Desktop-style command entry can point at
the same executable and set `MARGINALIA_HOME` / database settings through the
environment.

MCP exposes structured workflow tools including `ask_marginalia`,
`upload_file`, `download_file`, `download_folder`, `export_conversation`,
`search_files`, `get_file_metadata`, plus retrieval/source-reading tools such
as `recall_knowledge`, `search_metadata`, `search_journal`,
`read_entries_metadata`, and `read_files`.

## API Surface

Business endpoints live under `/v1`:

```text
POST /v1/upload
GET  /v1/search
GET  /v1/file-entries/{entry_id}/metadata
GET  /v1/file-entries/{entry_id}/content
POST /v1/sessions
POST /v1/chat/{session_id}          # Server-Sent Events
GET  /v1/conversations/{id}/export
POST /v1/tend
GET  /v1/tasks/active
GET  /v1/settings/llm
GET  /health
```

The desktop GUI and CLI both use the same API.

`POST /v1/chat/{session_id}` accepts `{ "query": "...", "mode": "deep" }`
or `{ "query": "...", "mode": "quick" }`. Omit `mode` for the default `auto`
planner-selected budget behavior.

## Configuration

Core `.env` fields:

```ini
MARGINALIA_HOME=~/Marginalia
DB_BACKEND=sqlite                  # sqlite or postgres
STORAGE_BACKEND=mirror             # mirror, local, or s3
WORKER_ENABLED=true
AUTO_LIFECYCLE_ENABLED=false
MAINTENANCE_DAILY_TOKEN_BUDGET=0  # rolling 24h background cap; 0 = unlimited
RELATION_BACKGROUND_VETTING_ENABLED=false

LLM_DEFAULT_PROVIDER=openai        # openai, openai-compatible, anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_BASE_URL=
LLM_DEFAULT_MODEL=gpt-4o-mini

LLM_CHAT_MODEL=
LLM_REFLECT_MODEL=
LLM_INGEST_MODEL=
LLM_VISION_MODEL=

EMBEDDING_API_KEY=
EMBEDDING_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
EMBEDDING_MODEL=text-embedding-v4
SEMANTIC_RECALL_ENABLED=false
SEMANTIC_INDEX_BACKEND=auto        # auto, file, sqlite-vec

RERANK_ENABLED=false
RERANK_API_KEY=
RERANK_BASE_URL=https://dashscope.aliyuncs.com/compatible-api/v1
RERANK_MODEL=qwen3-rerank
EVIDENCE_SELECTION=quota           # quota or rerank

AGENT_PLAN_MAX_TOKENS=1024
AGENT_EXECUTE_MAX_TOKENS=2048
AGENT_FINAL_ANSWER_CONTINUE_TURNS=3
AGENT_FINAL_ANSWER_MAX_CHARS=120000
```

Use `openai-compatible` for DeepSeek, Together, Groq, local vLLM, Ollama, and other OpenAI wire-compatible services.

The `vision` profile is optional. Without it, image enrichment, PDF figure captioning, and scanned-PDF OCR degrade gracefully or are skipped.

`MAINTENANCE_DAILY_TOKEN_BUDGET` is a rolling 24-hour cap for background
maintenance LLM usage. When it is exhausted, low-priority speculative tasks
(`restructure_catalogs`, `vet_relations`, `propose_views`) defer to a later
tick; foreground ingest and chat reflection are not limited.

Relation discovery is pure-read by default. Miners write cheap raw signals,
and `/discover` reads the already-vetted graph without calling an LLM. Use
`/discover <entry_id> --vet` (API: `vet=true`) to queue background vetting for
that seed's direct raw edges, or set `RELATION_BACKGROUND_VETTING_ENABLED=true`
if you want the periodic worker to batch-vet relation edges ahead of time.

When a long final answer hits the model token limit, Marginalia can continue it server-side and emit one merged answer event to the GUI. Tune `AGENT_FINAL_ANSWER_CONTINUE_TURNS` and `AGENT_FINAL_ANSWER_MAX_CHARS` for research-heavy deployments.

## Storage and Deployment

Default local layout:

```text
<MARGINALIA_HOME>/marginalia.db
<MARGINALIA_HOME>/library/
<MARGINALIA_HOME>/objects/
```

`STORAGE_BACKEND=mirror` stores files as a readable folder tree. `local` stores UUID-addressed objects. `s3` is for multi-host deployments.

Single-process mode:

```bash
marginalia
```

Remote API mode:

```bash
marginalia serve --host 0.0.0.0 --port 8000
marginalia --server http://server:8000
# If the server sets MARGINALIA_API_TOKEN:
marginalia --server http://server:8000 --api-token "$MARGINALIA_API_TOKEN"
```

Docker compose starts API, worker, Postgres, and MinIO:

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
```

The compose file binds the API and MinIO console to `127.0.0.1` by default.
If you deliberately expose the API on a LAN, set `MARGINALIA_API_TOKEN` and
send `Authorization: Bearer <token>` from the CLI or desktop connection
settings.

### Multi-device sync

Do not use Dropbox, Syncthing, iCloud Drive, OneDrive, or similar file-sync
tools to sync a live `MARGINALIA_HOME`. SQLite and the mirror/local storage
layout can be corrupted by concurrent replication. For multiple machines, use
the remote deployment shape with Postgres and S3-compatible object storage.

## Documentation

- [USAGE.md](USAGE.md): operations manual.
- [DESIGN.md](DESIGN.md): data model, retrieval design, task system, invariants.
- [samples/architecture.md](samples/architecture.md): developer architecture overview.
- [docs/LAUNCH.md](docs/LAUNCH.md): launch copy, social preview notes, and community post templates.

## Development

```bash
uv run ruff check src tests
.\.venv\Scripts\python -B -m pytest tests -q
```

Current tests cover upload, ingest, agent runtime, tool execution, export, task scheduling, PDF/DOCX/image/table/archive pipelines, relation discovery, lifecycle behavior, semantic index fallback, recall/rerank scoring, evaluation commands, and CLI flows.

## Community links
This open-source project is linked with and recognized by the LINUX DO community:

LINUX DO: [https://linux.do/](https://linux.do/)

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
