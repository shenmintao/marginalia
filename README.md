# Marginalia

> 中文版:[README.zh-CN.md](README.zh-CN.md)
> 设计文档:[DESIGN.md](DESIGN.md)

A library-science-inspired personal knowledge management system. AI does
the indexing, classification, and cross-referencing in the background.
You ask questions; an investigator agent reads its journal, gathers
context, and answers with citations.

**No vector DB. No embeddings. No chunk tuning.** Retrieval works
through structured access points (catalog tree / tags / views) +
metadata search + the agent reading raw files. The LLM provides the
semantic understanding; the schema provides the bookkeeping.

## How it works

Three roles, strictly separated:

- **🏛 Librarian** — offline batch. Ingests new files, normalizes tags,
  restructures the catalog. Writes most AI-internal state.
- **🔍 Investigator** — online agent. Plan → tool calls → answer with
  citations. Writes journal + observed relations after each turn.
- **👤 You** — upload, organize folders, archive, delete. The vault is
  yours; the AI's work product is separate.

The investigator's notebook is a real table (`journal`) the librarian
later reads when restructuring. That feedback loop is how the library
gets better with use.

Files are stored content-addressed (sha256). Each placement (folder +
display_name) gets its own AI fields (catalog, extra, tags), so the
same PDF in `/work` and `/research` can have independent
interpretations.

## Quickstart

```bash
python -m venv .venv
source .venv/Scripts/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

mkdir my-library && cd my-library
marginalia init                          # creates .env / data/ / .marginalia/
# edit .env: set LLM_DEFAULT_API_KEY
alembic upgrade head

marginalia
marginalia> /upload paper.pdf /
marginalia> compare raft and paxos
```

The `marginalia` command is one process — server, worker, and CLI all
inside it. No second terminal.

By default your files live as a real folder tree under
`~/Marginalia/library/...`. Browse them in Finder, back them up with
`rsync` or `git`, edit them in your editor — the vault IS your library;
marginalia just indexes it. After editing files outside marginalia, run
`/check` to diff and `/ingest --all` to sync.

`MARGINALIA_HOME=/some/path` relocates everything (db + library +
caches).

## CLI

`marginalia` is a Claude-Code-style REPL. `/`-prefix = slash command;
everything else goes to the agent.

```
/help                           list commands
/upload <local> <remote>        copy a file from outside the vault into it
/check                          diff vault disk vs db (read-only)
/ingest <vault_path>            sync one vault file with db
/ingest --all                   sync the whole vault
/discover <entry_id> [N]        show entries the corpus has linked to it
/tree                           folder tree
/ls [parent_id]                 list folders
/cd <path>                      change "remote cwd" for relative uploads
/search <query>                 find files by name + summary recall
/info <entry_id>                user-visible metadata + summary
/download <entry_id|folder_id>  file → bytes; folder → zip
/export [<conv_id>]             pack a conversation + citations as zip
/on-conflict rename|error|skip  set name-conflict policy
/clear  /  /new                 end / start a chat session
/quit
```

A chat turn renders as a state-driven event stream:

```
marginalia> compare raft and paxos
⠋ planning the investigation...
⠋ calling search_journal(q="raft consensus")
⠋ calling read_files(entry_id=...)
⠋ investigator thinking...
✓ answer ready

# Raft vs Paxos
Raft splits Paxos into three relatively independent sub-problems...
[^a]: entry_id=...

  [tokens in=3300 out=340 tools=2 llm_calls=3 4521ms]
```

## Architecture

**14 tables, 4 layers**:

```
audit_events                — event stream (90 d retention)
sessions / conversations    — containers + rolling counters
catalogs / views / tags /   — AI-internal: librarian's working knowledge
  tag_aliases / entry_tags /  (users never see these)
  entry_relations / journal
folders / file_entries /    — user-visible
  files
tasks / task_outcomes       — infrastructure
```

**11 task kinds, 13 agent tools, 8 ingest pipelines**:

- text / pdf (incl. scanned-PDF OCR via VLM) / image (with VLM downscaling)
- docx / spreadsheet / log (incl. logrotate variants)
- archive (zip / tar.* / 7z / rar / .gz / .bz2 / .xz / iso / cab / 50+ via py7zz)

### Discovery (cuts agent loop count)

Once the agent identifies one relevant entry, the discovery layer hands
it likely neighbours immediately — the next step doesn't burn another
search + read_files cycle. Three miners + an LLM gate populate
`entry_relations`; the random-walk service consumes the gated graph;
results are pre-filled into search and metadata responses.

```
mine_session_cooccurrence    journal notes group X and Y in the same conv
mine_tag_overlap             Jaccard ≥ 0.30 with ≥ 2 shared tags
mine_citation_graph          X and Y co-cited in the same agent answer
                ↓
       entry_relations (raw, source_kind tagged)
                ↓
   vet_relations              LLM gate per pair → vetted=True/False
                ↓
       entry_relations.vetted=True (clean graph)
                ↓
   services.recommend.find_related   random walk with restart, alpha=0.15
                ↓
   /discover <entry_id>            CLI surface
   search/get_metadata.related_entries   pre-filled top-3 / top-8
```

Miners + vet run on the periodic dispatcher (default daily; also kicked
by `/tend`). Random walk is query-time, read-only.

For full design see [`DESIGN.md`](DESIGN.md).

## API

All business endpoints under `/v1/`:

```
POST /v1/upload                        upload a file
GET  /v1/folders                       folder tree
GET  /v1/file-entries/{id}/...         per-file ops
GET  /v1/search                        metadata recall
POST /v1/sessions                      open a chat session
POST /v1/chat/{session_id}             chat (SSE stream)
POST /v1/sessions/{id}/close
GET  /v1/conversations/{id}/export     export conversation as zip
GET  /health                           liveness probe (unversioned)
```

`POST /v1/chat/{session_id}` returns `text/event-stream`. Events:
`conversation` / `planning` / `plan` / `thinking` / `tool_call` /
`tool_result` / `answer` / `error` / `done`. The CLI state machine
renders against these.

## Configuration

`.env`:

```ini
MARGINALIA_HOME=~/Marginalia     # one root; db + library + objects under here
DB_BACKEND=sqlite                # or postgres

STORAGE_BACKEND=mirror           # default. user-readable folder tree:
                                 #   <home>/library/research/llm/paper.pdf
                                 # alt: 'local' (UUID-flat, dedup on,
                                 # ~5x faster for high-churn) / 's3'

WORKER_ENABLED=true              # default in embedded mode

LLM_DEFAULT_PROVIDER=openai      # or anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o

MARGINALIA_SERVER=               # non-empty = remote mode, skip embedded
```

OpenAI-compatible endpoints (Together, Groq, DeepSeek, local vLLM /
ollama) supported via `LLM_*_BASE_URL`.

## Deployment

**Default (embedded)**: `marginalia` mounts FastAPI + TaskRunner in its
own process. HTTP never hits a socket — `httpx.ASGITransport` calls the
ASGI app directly. Right shape for 99% of usage.

```
   ┌──────────────────────────────────────┐
   │  marginalia  (CLI + ASGI + worker)   │
   └──────────────────────────────────────┘
```

**Multi-machine** (optional): split server into a standalone process,
CLIs HTTP into it. SQLite only allows one writer process at a time —
use Postgres for multi-machine.

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=true)
   └─────────────┘         └────────┬─────────┘
                                    │  shared Postgres + storage
```

```bash
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
marginalia --server http://server.lan:8000
# Or persist:  MARGINALIA_SERVER=http://server.lan:8000  in ~/.marginalia/.env
```

### Docker

`docker-compose.yml` brings up api + worker + Postgres + MinIO:

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
marginalia --server http://localhost:8000
```

Compose runs `alembic upgrade head` on api startup and creates the
MinIO bucket via a one-shot init container. Volumes (`pgdata`,
`miniodata`, `margdata`) persist across restarts.

## Development

```bash
.venv/Scripts/python tests/test_agent_e2e.py
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

35 e2e tests cover the full stack — upload, ingest, reflect, dispatcher,
purge, normalize_tags, enrich_tags, lifecycle, restructure, agent
runtime, agent tools, CLI, image / pdf / pdf-OCR / docx / spreadsheet /
container / git / archive pipelines, mirror storage, scan + sync, and
discovery.

## Status

v1: end-to-end functional, not yet hardened against real-world data.

Known gaps:

- No semantic / embedding retrieval. Recall is name + summary + tags +
  FTS5 against ingested text + the random-walk discovery layer.
  Adequate for personal libraries; not a vector-search replacement.
- Audio / video accepted but no pipeline. Speech-to-text is future.

## License

Copyright (c) 2026 shenmintao

AGPL-3.0-or-later. See [LICENSE](LICENSE).

If you run a modified Marginalia as a network service, the AGPL
requires you to make the corresponding source available to your users.
