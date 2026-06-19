# Marginalia Operations Manual

> Chinese manual: [USAGE.zh-CN.md](USAGE.zh-CN.md)
> Design rationale: [DESIGN.md](DESIGN.md)

This manual describes how to install, configure, run, evaluate, and
troubleshoot Marginalia as a private heterogeneous knowledge-base retrieval
and report-generation system.

## 1. Install

Requires Python 3.11+.

```bash
git clone <repo>
cd Marginalia
python -m venv .venv

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate

pip install -e ".[dev]"
```

Check the CLI:

```bash
marginalia --help
```

## 2. Initialize a Library

```bash
mkdir my-library
cd my-library
marginalia init
```

`init` creates a starter `.env` and local folders. Runtime state is rooted at `MARGINALIA_HOME`; when unset it defaults to `~/Marginalia`.

Recommended explicit setting:

```ini
MARGINALIA_HOME=E:/Marginalia
```

## 3. Configure LLM Profiles

Minimal `.env`:

```ini
LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
```

OpenAI-compatible providers such as DeepSeek, Together, Groq, vLLM, or Ollama:

```ini
LLM_DEFAULT_PROVIDER=openai-compatible
LLM_DEFAULT_BASE_URL=https://api.deepseek.com/v1
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=deepseek-chat
```

Profiles:

```ini
LLM_CHAT_MODEL=              # online investigator
LLM_REFLECT_MODEL=           # journal reflection after each turn
LLM_INGEST_MODEL=            # ingest and background maintenance
LLM_VISION_MODEL=            # images, PDF figures, scanned-PDF OCR
```

Unset profile fields inherit from `LLM_DEFAULT_*`. `chat`, `reflect`, and `ingest` must resolve to an API key. `vision` is optional.

The desktop Settings page can write LLM overrides to `config_overlay.json`; those values take precedence over `.env` LLM fields.

Long research answers are continued server-side if the final answer hits the
model token limit. The GUI receives one merged `answer` event.

```ini
AGENT_EXECUTE_MAX_TOKENS=2048
AGENT_FINAL_ANSWER_CONTINUE_TURNS=3
AGENT_FINAL_ANSWER_MAX_CHARS=120000
```

## 4. Start Marginalia

Embedded mode:

```bash
marginalia
```

This starts FastAPI, TaskRunner, and the CLI in one process. Database schema bootstrap runs automatically on startup.

Remote server mode:

```bash
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
marginalia --server http://127.0.0.1:8000
```

`alembic upgrade head` is still safe for explicit migration workflows, but a fresh local database does not require a separate migration step before first use.

## 5. First Complete Flow

Upload:

```text
marginalia> /upload ./papers/raft.pdf /papers/
```

Watch ingest:

```text
marginalia> /background
```

Find the entry:

```text
marginalia> /search raft
marginalia> /info <entry_id>
```

Ask a question:

```text
marginalia> compare this Raft paper with my Paxos notes
```

The investigator will plan, call `recall_knowledge` for broad material
location, inspect candidate metadata, read original file slices, and answer
with footnotes.

The desktop chat composer has a per-turn **Quick / Deep** switch. Quick keeps
the plan phase but caps execute to at most two evidence-gathering passes
followed by a forced answer on the third execute call, which is useful for
lookup-style questions. Deep is the default full investigation loop.

Export:

```text
marginalia> /export
```

## 6. Capability Profile

Marginalia is intentionally stronger than a plain top-k RAG loop in the
personal-library investigation case:

- it keeps durable journal memory from previous investigations;
- it searches structured metadata, tags, folders, catalogs, and views before
  reading raw files;
- it can add optional embedding recall and reranking without making vectors
  the only retrieval path;
- it follows related-entry signals and reads original source windows before
  making cited claims;
- it can compare the full ReAct report workflow against one-shot RAG with
  `marginalia eval compare-report`.

Metadata text search is indexed in both local and remote database modes:
SQLite uses the local FTS5 trigram table, while Postgres uses native
`to_tsvector` / `websearch_to_tsquery` expression GIN indexes. Chinese short
terms that are too small for trigram tokenization are kept through a bounded
LIKE fallback in mixed metadata queries.
Journal recall validates referenced entries when it is read: notes that point
at deleted entries or files reprocessed after the note was written are kept
for audit, marked stale, and ranked behind current notes. Later reflections
can also mark directly contradicted journal rows invalidated; active recall
hides them unless `search_journal` is called with `include_invalidated=true`.

The current evidence supports advertising it as a strong personal-library
research agent, especially for source-grounded reports. It should not be
described as general SOTA across all RAG benchmarks. The validated claim is
narrower and more useful: on local SciFact evaluation, the ReAct workflow beat
a one-shot RAG report baseline in most sampled end-to-end comparisons, at the
cost of higher latency and more LLM calls.

Latest local validation:

- SciFact 300 retrieval with `recall_knowledge` + rerank top-80: MRR 0.7226,
  hit@10 0.8800, hit@100 0.9133.
- SciFact 300 bounded answer-run with rerank top-80 and quota selection:
  evidence hit 0.8667, citation hit 0.7133, label accuracy 0.8085.
- 30-query end-to-end report comparison: ReAct won 26, one-shot RAG won 2,
  tied 2, with 1 timeout.

## 7. CLI Commands

### Files and Folders

```text
/upload <local> <remote>       upload file or directory into the vault
/check                         read-only mirror vault diff
/ingest <vault_path>           sync one existing vault file
/ingest --all                  apply all /check changes
/tree [depth]                  folder tree
/ls [folder_id]                list folders
/cd <remote_path>              set remote cwd
/download <id> [dest]          download file or folder zip
```

Use `/upload` for files outside the vault. Use `/ingest` for files already inside the mirror vault.

### Search and Read

```text
/search <query>                metadata recall
/info <entry_id>               metadata and preview
/discover <entry_id> [N]       vetted related entries
/discover <entry_id> --all     include unvetted relation signals
/discover <entry_id> --vet     queue background vetting for direct signals
```

Any non-slash input is sent to the agent.

### Retrieval Evaluation

`marginalia eval` is a non-interactive command group for external retrieval
benchmarks. It currently imports local BEIR-style datasets:

```text
corpus.jsonl
queries.jsonl
qrels/test.tsv
```

Import runs ingest synchronously for every corpus document. Use
`--concurrency` for large corpora and `--resume` after an interrupted import:

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval import-beir scifact ./datasets/scifact --concurrency 100 --resume
```

Build semantic recall with Bailian/DashScope `text-embedding-v4`:

```bash
MARGINALIA_HOME=./runtime/eval/scifact EMBEDDING_API_KEY=... marginalia eval build-semantic-index scifact --concurrency 10 --resume
```

Run retrieval metrics after import:

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever search_metadata --k 10,50,100
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever semantic_recall --k 10,50,100
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever recall_knowledge --json report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval ablation-run scifact --k 10,50,100 --json ablation-report.json
```

Probe one final answer with a hard wall-clock budget:

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval answer scifact --retriever recall_knowledge --query-id <qid> --timeout-seconds 300 --json answer-report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval answer-run scifact --retriever recall_knowledge --qrels-only --query-limit 20 --concurrency 10 --timeout-seconds 300 --json answer-run-report.json
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval compare-report scifact --query-limit 30 --concurrency 3 --timeout-seconds 300 --json compare-report.json
```

Reported metrics distinguish candidate-pool recall from ranking efficiency:
hit@k answers whether at least one relevant document entered the candidate
pool, candidate_recall@k answers how much labeled evidence entered it, and
nDCG/MRR describe how early evidence appears. Use a dedicated
`MARGINALIA_HOME` for external benchmarks to avoid mixing benchmark documents
with your personal library. `eval ablation-run` runs a retrieval component
matrix over metadata-only, relations, semantic recall, and rerank variants,
then reports per-configuration deltas against metadata-only. `eval answer`
does not run the open-ended chat
agent loop; it retrieves, reads bounded evidence, makes one final-answer LLM
call, and reports whether the answer cited a qrels-relevant document. Use
`eval answer-run` to repeat that bounded probe across dataset queries and get
an aggregate final-answer citation hit rate. Add `--qrels-only` when
`--query-limit` should count evaluated qrels-backed queries, and
`--concurrency` to run independent answer probes in parallel. When query
metadata includes SciFact-style SUPPORT/CONTRADICT labels, the report also
includes label accuracy.
Use `eval compare-report` when you want to compare the bounded one-shot RAG
report path against the full ReAct investigation workflow. It runs both
systems on the same queries and uses a blind pairwise judge; when gold verdict
labels are present, correctness is judged before completeness.

When a semantic index exists under `MARGINALIA_HOME/semantic-index/default`,
`recall_knowledge` can merge semantic candidates with the existing FTS/BM25
metadata path. Semantic recall is optional and disabled by default; enable it
with `SEMANTIC_RECALL_ENABLED=true` after building an index. The embedding
provider defaults to Bailian/DashScope `text-embedding-v4`; configure it with
`EMBEDDING_API_KEY`. Embedding credentials are intentionally separate from
`LLM_*` profiles. The current public build command indexes imported eval
datasets; a whole-library index command is not exposed yet, so only entries
present in the default semantic index participate in semantic recall. If
`sqlite-vec` is installed through
`pip install -e ".[semantic]"`, the index writes `vectors.sqlite` and semantic
search uses it before falling back to the file index. Set
`SEMANTIC_INDEX_BACKEND=file` to avoid sqlite-vec entirely.
Optional second-stage reranking runs after hybrid recall and before evidence
selection. Enable it with `RERANK_ENABLED=true` and `RERANK_API_KEY=...`;
the default model is Bailian/DashScope `qwen3-rerank`. Rerank credentials are
independent from `LLM_*` and embedding settings. `EVIDENCE_SELECTION=quota`
keeps source quotas; `EVIDENCE_SELECTION=rerank` disables quotas and reads the
reranked top evidence directly.

### Sessions and Export

```text
/new                           open a new session
/clear                         close current session
/export [conversation_id]      export latest or selected conversation
/quit                          exit
```

### Background Maintenance

```text
/background                    active and pending tasks
/tend                          trigger one maintenance chain
/tend <run_id>                 inspect a maintenance run
```

Maintenance includes tag quality, catalog restructuring, lifecycle suggestions,
relation mining, view proposals, entry-extra refresh, and pruning. Relation
discovery is pure-read by default: `/discover` reads the already-vetted graph
without calling an LLM. Use `/discover <entry_id> --vet` to queue background
vetting for that seed's direct raw edges, or set
`RELATION_BACKGROUND_VETTING_ENABLED=true` to let the periodic worker pre-vet
relation edges ahead of time.

### MCP Server

Run a stdio MCP server when you want Claude Desktop or another MCP-capable
agent to use Marginalia as a private-library backend:

```bash
marginalia mcp
# or
marginalia-mcp
```

Only read-only retrieval tools are exposed: `recall_knowledge`, `read_files`,
`search_metadata`, `search_journal`, `read_entries_metadata`, `list_folder`,
`list_catalogs`, `read_catalog`, `resolve_tag`, and `materialize_view`.
Write-side tools and artifact generators are intentionally absent from the MCP
surface. Configure the MCP client with the same `MARGINALIA_HOME`, database,
storage, and optional provider environment variables you use for the CLI.

## 8. Asking Effective Questions

Marginalia works best for questions that need evidence from your library:

```text
Which saved contracts make the bonus discretionary?
Which papers discuss Byzantine fault tolerance?
Group my observability notes by product risk.
```

The agent is prompted to:

1. use `recall_knowledge` for broad material location;
2. preserve exact names, dates, numbers, and file-like phrases as recall terms;
3. batch-check candidate metadata before reading raw files;
4. use lower-level search tools for focused follow-up;
5. read original files before making source-backed claims.

PDF citations prefer exact `quote` lookup over page-only lookup, because printed page labels can differ from physical PDF pages.

## 9. Read Granularity

`read_files` supports:

- generic byte/character windows: `offset`, `max_chars`;
- text: `section_id`, `heading`, `line_start`, `line_end`, `pattern`;
- PDF: `page_start`, `page_end`, `page_label`, `pattern`;
- DOCX: `paragraph_start`, `paragraph_end`;
- archive: `member_path`.

Long documents are windowed. Default PDF reads do not extract an entire thousand-page document; results include continuation hints such as `next_page_start`. For long text, default reads are proportional to the requested window, while deep reads can scan more when searching by heading, section, line, or pattern.

## 10. Storage Backends

### mirror

Default. Files live in a readable tree:

```text
<MARGINALIA_HOME>/library/papers/raft.pdf
```

If you edit files outside Marginalia:

```text
/check
/ingest --all
```

### local

UUID-addressed object pool. Faster for high-churn workloads, less friendly for direct browsing.

Migration:

```bash
marginalia storage migrate --from mirror --to local
marginalia storage migrate --from local --to mirror
```

### s3

Remote object storage for multi-host deployments. Use Postgres with S3; SQLite is not suitable for multiple writer processes.

### Multi-device sync

Do not sync a live `MARGINALIA_HOME` with Dropbox, Syncthing, iCloud Drive,
OneDrive, or similar tools. SQLite databases and the mirror/local storage
layout are not safe under concurrent file replication. Stop Marginalia before
copying the directory for backup; for active multi-device use, run a remote
server with Postgres and S3-compatible object storage.

## 11. Lifecycle

Entries can be:

- `active`
- `demoted`
- `archived`
- `manual_active`
- `manual_archived`

Automatic lifecycle transitions are off by default:

```ini
AUTO_LIFECYCLE_ENABLED=false
MAINTENANCE_DAILY_TOKEN_BUDGET=0
RELATION_BACKGROUND_VETTING_ENABLED=false
```

This is deliberate for personal libraries. Shared deployments can enable
automatic lifecycle changes, cap background LLM maintenance tokens, or opt into
periodic relation pre-vetting.

## 12. Troubleshooting

### Missing LLM API key

Set `LLM_DEFAULT_API_KEY`, or per-profile keys:

```ini
LLM_CHAT_API_KEY=...
LLM_REFLECT_API_KEY=...
LLM_INGEST_API_KEY=...
```

### Semantic recall does nothing

Semantic recall is opt-in. Build an index first, then enable it:

```bash
pip install -e ".[semantic]"
MARGINALIA_HOME=./runtime/eval/scifact EMBEDDING_API_KEY=... marginalia eval build-semantic-index scifact
```

```ini
SEMANTIC_RECALL_ENABLED=true
EMBEDDING_API_KEY=...
SEMANTIC_INDEX_BACKEND=auto
```

Embedding credentials are not inferred from `LLM_DEFAULT_API_KEY` or
`LLM_VISION_API_KEY`. The current CLI build command is for imported eval
datasets; if you have not built a default index containing the target entries,
semantic recall will correctly fall back to lexical metadata recall.

### Rerank is configured but not used

Rerank only runs when `RERANK_ENABLED=true`, `RERANK_API_KEY` is set, and the
recall call has text terms to score:

```ini
RERANK_ENABLED=true
RERANK_API_KEY=...
RERANK_MODEL=qwen3-rerank
RERANK_TOP_N=80
EVIDENCE_SELECTION=quota
```

Keep `EVIDENCE_SELECTION=quota` when you want diversity across overlapping,
tag, lexical, and semantic signals. Use `EVIDENCE_SELECTION=rerank` only when
you want the reranked order to directly decide the evidence set.

### File stuck in `processing`

```text
/info <entry_id>
/background
```

`recover_stuck_tasks` runs periodically. You can also trigger maintenance:

```text
/tend
```

### Scanned PDF has no text

Configure a vision profile:

```ini
LLM_VISION_PROVIDER=openai
LLM_VISION_API_KEY=...
LLM_VISION_MODEL=gpt-4o
```

Without vision, scanned PDFs are marked as needing OCR instead of producing misleading empty text.

### PDF and image indexing limits

PDF ingest keeps the original file, but the ingest-time LLM index is bounded:

| Behavior | Value | Effect |
|----------|-------|--------|
| Text-layer ingest cap | 400 pages | For text-layer PDFs, later pages are omitted from the ingest-time summary/index; the stored PDF can still be read by explicit page range. |
| Chunked indexing trigger | >60 indexed pages or >80 KB rendered context | Long PDFs switch from one prompt to per-chunk indexing; this is not data loss by itself. |
| Chunk size | 40 pages, then smaller if needed | Each chunk is shrunk until its rendered prompt is under 80 KB when possible. |
| Oversize single chunk/page | 80 KB rendered context | If a chunk cannot be reduced further, only that prompt chunk is truncated and coverage records `prompt_text_cap` / `truncated_chunks`. |
| Scanned-PDF detection | <50 text chars/page on average | With a vision profile, ingest falls back to OCR. Without vision, the file is marked as needing OCR rather than indexed as empty text. |
| OCR ingest cap | `OCR_MAX_PAGES` when configured | By default OCR processes all pages; if a positive cap is configured, later OCR pages are omitted and coverage records `ocr_page_cap`. |

Embedded PDF image captions are optional enrichment and only run when a vision
profile is configured and the PDF is not in OCR mode:

| Behavior | Value | Effect |
|----------|-------|--------|
| Images per PDF page | 5 | More embedded images on the same page are skipped. |
| Images per PDF document | 30 | Later embedded images are skipped. |
| Minimum embedded image | 512 bytes and, when dimensions are known, 100 px on each side | Smaller images are filtered before captioning. |
| Bytes sent per embedded image | 4 MB | Larger extracted images are clipped before the VLM caption call. |

Standalone raster image ingest has a different path: it requires a vision
profile, reads at most 10 MB from the image file, downscales/re-encodes to JPEG
with a 1568 px long edge, and indexes the VLM description.

PDF read-time windows are pagination limits, not ingest loss:

| Behavior | Value | Effect |
|----------|-------|--------|
| Default PDF read | 20 pages | Used when no page range is requested. |
| Max PDF pages per read call | 50 pages | Explicit page windows are capped per `read_files` call. |
| Unscoped PDF pattern search | 200 pages | Pattern search without an explicit page range searches only the prefix. |

Coverage metadata is stored under `description.coverage` and surfaced in
metadata/search JSON where available. Important fields include
`indexed_partial`, `partial_reasons`, `indexed_pages`, `total_pages`,
`max_index_pages`, `chunked`, `chunk_count`, `text_truncated`, `ocr_used`,
and `truncated_chunks`.

### Storage backend mismatch

If startup reports that existing `storage_key` values do not match the configured backend, either restore the previous backend or migrate:

```bash
marginalia storage migrate --from local --to mirror
```

### Re-index a changed file

For mirror storage:

```text
/check
/ingest <path>
```

## 13. Backup

For SQLite + mirror/local storage, stop Marginalia and copy the whole `MARGINALIA_HOME` directory. This is a backup operation, not live multi-device sync.

Windows:

```bash
robocopy E:\Marginalia D:\backup\Marginalia /MIR
```

macOS/Linux:

```bash
rsync -a ~/Marginalia/ /backup/Marginalia/
```

For Postgres/S3 deployments, back up the database and object storage separately.
