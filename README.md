# Marginalia

> 中文版：[README.zh-CN.md](README.zh-CN.md)

A library-science-inspired personal knowledge management system with LLM
agents. You upload documents; a librarian agent quietly indexes,
classifies, and cross-references them in the background. When you have
a question, an investigator agent reads its journal of past work,
gathers the right context, and answers with citations.

## Why "library science"

Most "AI search over your files" systems are retrieval-augmented Q&A —
the AI is a passive consumer. Marginalia treats the AI as the librarian:
it owns the catalog tree, the tags, the cross-references, and the
journal. Your files keep their human-curated folder layout; everything
else (catalog, tags, relations, summaries) belongs to the agent and is
shaped by use over time.

## Quickstart

```bash
# 1. install
python -m venv .venv
source .venv/Scripts/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                     # then edit LLM_DEFAULT_API_KEY
alembic upgrade head

# 2. run server + worker (two processes is the production layout)
uvicorn marginalia.main:app               # terminal 1
marginalia-worker                         # terminal 2

# 3. seed sample data
python samples/seed.py

# 4. talk to it
marginalia
marginalia> /tree
marginalia> /search consensus
marginalia> compare raft and paxos
```

## What the CLI looks like

`marginalia` is a Claude-Code-style REPL. Anything starting with `/` is
a slash command; everything else is forwarded to the agent as chat.

```
/help                                  list commands
/upload <local> <remote>               trailing '/' = folder, with extension = filename
/upload <local> <remote> --name X      explicit display name
/tree                                  folder tree
/ls [parent_id]                        list folders
/cd <path>                             change "remote cwd" for relative uploads
/search <query>                        find files by name + summary recall
/info <entry_id>                       user-visible metadata + summary
/download <entry_id|folder_id>         file → bytes; folder → zip
/export [<conv_id>]                    pack a conversation + its citations as zip
/on-conflict rename|error|skip         set name-conflict policy
/clear / /new                          end / start a chat session
/quit
```

## Architecture in one breath

```
Five layers (data):
  audit_events            event stream for human auditors only
  sessions/conversations  containers + rolling counters
  AI-internal             catalogs / tags / journal / entry_relations
  user-visible            folders / file_entries / files
  infrastructure          tasks / task_outcomes
```

```
Three LLM roles (writers):
  🔍 investigator  online agent — reads journal, calls tools, answers
  🏛 librarian     offline batch — ingest, normalize_tags, restructure...
  📋 reflector     after each turn — writes journal so future you can
                   find this thread again
```

```
12 tasks, 12 tools, 3 ingest pipelines (text / image / pdf-with-figures)
```

For full design, see [`design.md`](design.md). For an architectural
overview shipped with the samples: `samples/architecture.md`.

## Configuration

All settings via `.env`. Highlights:

```ini
DB_BACKEND=sqlite                # or postgres
SQLITE_PATH=./data/marginalia.db

STORAGE_BACKEND=local            # or s3
LOCAL_STORAGE_ROOT=./data/objects

WORKER_ENABLED=false             # true = run TaskRunner in API process (dev)

LLM_DEFAULT_PROVIDER=openai      # or anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
# Per-profile overrides (chat / reflect / ingest / vision / audio):
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o
```

OpenAI-compatible endpoints (Together, Groq, DeepSeek, local vLLM /
ollama) are supported via `LLM_*_BASE_URL`.

## Deployment shape

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=false)
   └─────────────┘         └────────┬─────────┘
                                    │  shared DB + storage
                                    │
                            ┌───────▼────────────┐
                            │ marginalia-worker  │  (TaskRunner)
                            └────────────────────┘
```

## Development

```bash
# run any single end-to-end test
.venv/Scripts/python tests/test_agent_e2e.py

# run all e2e tests
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

20 e2e tests cover upload, ingest, reflect, dispatcher, purge,
normalize_tags, enrich_tags, lifecycle, restructure, agent runtime,
agent tools, user mgmt, CLI, image pipeline, user files, export, pdf,
pdf-with-images, duckdb tools, and worker daemon.

## Status

Marginalia is at v1: end-to-end functional but not yet hardened against
real-world data. Known gaps:

- Scanned PDFs are flagged `needs_ocr` and skipped (no OCR pipeline yet).
- Container files (zip / tar / git repos) are accepted but have no
  pipeline yet — they sit at `ingest_status='pending'`.
- Recommendation-style background mining (cooccurrence, random walk) is
  on the next-cycle list.

## License

(insert license file here)
