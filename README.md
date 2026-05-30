# Marginalia

> Chinese README: [README.zh-CN.md](README.zh-CN.md)
> Detailed design: [DESIGN.md](DESIGN.md)

![Marginalia promotional hero](docs/images/marginalia-promo-en.png)

Marginalia is an AI retrieval infrastructure for private heterogeneous knowledge bases. It is designed for personal or small-team libraries made of PDFs, notes, office documents, images, spreadsheets, logs, archives, and evolving investigation history.

The core idea is not "embed everything and search vectors." Marginalia uses a structured retrieval funnel:

1. narrow the search space with folders, catalogs, tags, views, metadata, and persistent investigation journals;
2. discover neighbouring evidence through relation-mining and recommendation-style graph traversal;
3. read the original file at the relevant section, line, page, paragraph, archive member, or table slice;
4. answer with citations that point back to source entries and, where possible, exact quotes or PDF pages.

This gives the LLM a controlled way to work inside a private library: recall prior investigations, inspect candidates, verify facts against originals, and leave behind durable notes for future turns.

## What Marginalia Provides

- **Private heterogeneous library**: text, Markdown, PDFs, DOCX, images, spreadsheets, logs, and archives live in one searchable system.
- **Structured funnel retrieval**: catalog tree, tags, views, metadata, journal recall, and targeted file reads replace ad hoc chunk retrieval.
- **Persistent investigation journal**: every completed turn can write a compact `journal` entry that future planning can search.
- **Recommendation-style evidence discovery**: background miners populate `entry_relations` from session co-occurrence, tag overlap, citation co-citation, and corpus evidence; LLM vetting filters noisy edges; query-time random walk surfaces related entries.
- **Original-source verification**: answers cite `entry_id`, optional verbatim `quote`, optional PDF physical `page`, and a reason. PDF quote lookup can correct page offsets caused by covers or tables of contents.
- **Local-first storage**: default mirror storage keeps files in a normal folder tree under `MARGINALIA_HOME/library`.

## Quickstart

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

## The Retrieval Funnel

```text
user question
  -> plan
  -> search_journal              # prior investigations
  -> search_metadata/list_folder # names, summaries, tags, catalogs
  -> read_entries_metadata       # sections, extra, related entries
  -> discover/related entries    # graph-based neighbours
  -> read_files                  # original text/page/line/member/table slice
  -> answer with footnotes
  -> reflect_turn                # durable journal memory
```

The agent is instructed to start substantive questions with `search_journal`. For multi-keyword journal lookup it tries `search_journal(tags=[...])` first for OR-style tag recall, then falls back to `search_journal(text=...)` when needed.

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
default; set `SEMANTIC_RECALL_ENABLED=true` to merge semantic candidates with
the lexical metadata recall path. If the optional `sqlite-vec` dependency is
installed, the semantic index also writes `vectors.sqlite` and search uses it
before falling back to the file index. Install with `pip install -e ".[semantic]"`,
or set `SEMANTIC_INDEX_BACKEND=file` to keep only the file backend.
Optional reranking can refine the merged candidate pool before evidence
selection. Enable it with `RERANK_ENABLED=true`, `RERANK_API_KEY=...`, and
optionally `RERANK_MODEL=qwen3-rerank`. Rerank credentials are also separate
from `LLM_*`; no chat or vision key is reused implicitly. Evidence selection
defaults to `EVIDENCE_SELECTION=quota`; set `EVIDENCE_SELECTION=rerank` to take
the reranked top evidence directly.
The eval report treats `hit@k` and `candidate_recall@k` as the investigation
candidate-pool metrics; MRR and nDCG are ranking-efficiency diagnostics.
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

## CLI Surface

Slash commands:

```text
/help                         list commands
/upload <local> <remote>      upload a file or directory into the vault
/check                        diff mirror vault vs database
/ingest <path> | --all        sync manual vault edits into the database
/search <query>               metadata recall
/info <entry_id>              entry metadata and preview
/discover <entry_id> [N]      related entries from the evidence graph
/tree                         folder tree
/download <id> [dest]         download file or folder zip
/export [conversation_id]     export answer and citations
/tend                         run a maintenance pass
/background                   show queued/running tasks
/new / /clear / /quit         session control
```

Any non-slash input is sent to the investigator agent.

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

## Configuration

Core `.env` fields:

```ini
MARGINALIA_HOME=~/Marginalia
DB_BACKEND=sqlite                  # sqlite or postgres
STORAGE_BACKEND=mirror             # mirror, local, or s3
WORKER_ENABLED=true
AUTO_LIFECYCLE_ENABLED=false

LLM_DEFAULT_PROVIDER=openai        # openai, openai-compatible, anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_BASE_URL=
LLM_DEFAULT_MODEL=gpt-4o-mini

LLM_CHAT_MODEL=
LLM_REFLECT_MODEL=
LLM_INGEST_MODEL=
LLM_VISION_MODEL=

AGENT_PLAN_MAX_TOKENS=1024
AGENT_EXECUTE_MAX_TOKENS=2048
AGENT_FINAL_ANSWER_CONTINUE_TURNS=3
AGENT_FINAL_ANSWER_MAX_CHARS=120000
```

Use `openai-compatible` for DeepSeek, Together, Groq, local vLLM, Ollama, and other OpenAI wire-compatible services.

The `vision` profile is optional. Without it, image enrichment, PDF figure captioning, and scanned-PDF OCR degrade gracefully or are skipped.

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
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
marginalia --server http://server:8000
```

Docker compose starts API, worker, Postgres, and MinIO:

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
```

## Documentation

- [USAGE.md](USAGE.md): operations manual.
- [DESIGN.md](DESIGN.md): data model, retrieval design, task system, invariants.
- [samples/architecture.md](samples/architecture.md): developer architecture overview.

## Development

```bash
.\.venv\Scripts\python -B -m pytest tests -q
```

Current tests cover upload, ingest, agent runtime, tool execution, export, task scheduling, PDF/DOCX/image/table/archive pipelines, relation discovery, lifecycle behavior, and CLI flows.

## Community links
This open-source project is linked with and recognized by the LINUX DO community:

LINUX DO: [https://linux.do/](https://linux.do/)

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
