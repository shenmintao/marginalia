# Marginalia Operations Manual

> Chinese manual: [USAGE.zh-CN.md](USAGE.zh-CN.md)
> Design rationale: [DESIGN.md](DESIGN.md)

This manual describes how to install, configure, run, and troubleshoot Marginalia as a private heterogeneous knowledge-base retrieval system.

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

The investigator will plan, search journal, search metadata, inspect candidates, read original file slices, and answer with footnotes.

Export:

```text
marginalia> /export
```

## 6. CLI Commands

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

Import runs ingest synchronously for every corpus document:

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval import-beir scifact ./datasets/scifact
```

Build semantic recall with Bailian/DashScope `text-embedding-v4`:

```bash
MARGINALIA_HOME=./runtime/eval/scifact EMBEDDING_API_KEY=... marginalia eval build-semantic-index scifact
```

Run retrieval metrics after import:

```bash
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever search_metadata --k 10,50,100
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever semantic_recall --k 10,50,100
MARGINALIA_HOME=./runtime/eval/scifact marginalia eval run scifact --retriever recall_knowledge --json report.json
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
with your personal library. `eval answer` does not run the open-ended chat
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
`LLM_*` profiles. If `sqlite-vec` is installed through
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

Maintenance includes tag quality, catalog restructuring, lifecycle suggestions, relation mining, relation vetting, view proposals, entry-extra refresh, and pruning.

## 7. Asking Effective Questions

Marginalia works best for questions that need evidence from your library:

```text
Which saved contracts make the bonus discretionary?
Which papers discuss Byzantine fault tolerance?
Group my observability notes by product risk.
```

The agent is prompted to:

1. start substantive turns with `search_journal`;
2. for multi-keyword journal recall, try `search_journal(tags=[...])` first;
3. fall back to `search_journal(text=...)` when tag recall is weak;
4. read original files before making source-backed claims.

PDF citations prefer exact `quote` lookup over page-only lookup, because printed page labels can differ from physical PDF pages.

## 8. Read Granularity

`read_files` supports:

- generic byte/character windows: `offset`, `max_chars`;
- text: `section_id`, `heading`, `line_start`, `line_end`, `pattern`;
- PDF: `page_start`, `page_end`, `page_label`, `pattern`;
- DOCX: `paragraph_start`, `paragraph_end`;
- archive: `member_path`.

Long documents are windowed. Default PDF reads do not extract an entire thousand-page document; results include continuation hints such as `next_page_start`. For long text, default reads are proportional to the requested window, while deep reads can scan more when searching by heading, section, line, or pattern.

## 9. Storage Backends

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

## 10. Lifecycle

Entries can be:

- `active`
- `demoted`
- `archived`
- `manual_active`
- `manual_archived`

Automatic lifecycle transitions are off by default:

```ini
AUTO_LIFECYCLE_ENABLED=false
```

This is deliberate for personal libraries. Shared deployments can enable it to let background tasks demote or archive inactive material.

## 11. Troubleshooting

### Missing LLM API key

Set `LLM_DEFAULT_API_KEY`, or per-profile keys:

```ini
LLM_CHAT_API_KEY=...
LLM_REFLECT_API_KEY=...
LLM_INGEST_API_KEY=...
```

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

## 12. Backup

For SQLite + mirror/local storage, stop Marginalia and copy the whole `MARGINALIA_HOME` directory.

Windows:

```bash
robocopy E:\Marginalia D:\backup\Marginalia /MIR
```

macOS/Linux:

```bash
rsync -a ~/Marginalia/ /backup/Marginalia/
```

For Postgres/S3 deployments, back up the database and object storage separately.
