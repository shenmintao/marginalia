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

# 2. initialize a working directory (wherever you want your library)
mkdir my-library && cd my-library
marginalia init                           # creates .env / data/ / .marginalia/
# Then edit .env to set LLM_DEFAULT_API_KEY
alembic upgrade head

# 3. open marginalia
marginalia
marginalia> /upload paper.pdf /
marginalia> compare raft and paxos
```

The `marginalia` command is one process — server, worker, and CLI all
run inside it (same shape as Claude Code or the DeepSeek TUI). No need
to open two terminals.

By default your files live as a real folder tree under
`~/Marginalia/library/research/llm/paper.pdf`. Browse them in Finder,
back them up with `rsync` or `git`, edit them in your favourite editor —
the vault IS your library, marginalia just indexes it. After you change
files outside marginalia, run `/check` to see the diff and `/ingest --all`
to sync db with disk. Set `MARGINALIA_HOME=/some/path` to relocate the
whole footprint (db + library + caches) to wherever you want.

When you want to share one library across machines (laptop + desktop),
split the server out as a separate process and point the CLI at it via
`--server URL`. See "Deployment shape" below.

## What the CLI looks like

`marginalia` is a Claude-Code-style REPL. Anything starting with `/` is
a slash command; everything else is forwarded to the agent as chat.

```
/help                                  list commands
/upload <local> <remote>               copy a file from OUTSIDE the vault into it
/upload <local> <remote> --name X      explicit display name
/check                                  diff vault disk vs db (read-only)
/ingest <vault_path>                    sync one vault file with db
/ingest --all                           sync the whole vault (git add -A style)
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

Chatting with the agent isn't a dead spinner — it's a state-driven
event stream:

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
12 tasks, 12 tools, 8 ingest pipelines
  text / pdf (incl. scanned-PDF OCR via VLM) / image (with VLM downscaling)
  docx / spreadsheet / log (incl. logrotate variants)
  archive (zip / tar.* / 7z / rar / .gz / .bz2 / .xz / iso / cab / 50+ via py7zz)
```

For full design, see [`design.md`](design.md). For an architectural
overview shipped with the samples: `samples/architecture.md`.

## API

All business endpoints live under `/v1/`:

```
POST /v1/upload                         upload a file
GET  /v1/folders                        folder tree
GET  /v1/file-entries/{id}/...          per-file ops
GET  /v1/search                         metadata recall
POST /v1/sessions                       open a chat session
POST /v1/chat/{session_id}              chat (SSE stream)
POST /v1/sessions/{id}/close            close a session
GET  /v1/conversations/{id}/export      export conversation as zip
GET  /health                            liveness probe (unversioned, by convention)
```

`POST /v1/chat/{session_id}` returns `text/event-stream` with these
event types: `conversation` / `planning` / `plan` / `thinking` /
`tool_call` / `tool_result` / `answer` / `error` / `done`. The CLI
state machine renders against these.

## Configuration

All settings via `.env`. Highlights:

```ini
MARGINALIA_HOME=~/Marginalia     # one root; db + library + objects under here
DB_BACKEND=sqlite                # or postgres
SQLITE_PATH=                     # blank → <home>/marginalia.db

STORAGE_BACKEND=mirror           # default. user-readable folder tree under
                                 # <home>/library/research/llm/paper.pdf
                                 # → use /check + /ingest to round-trip with disk.
                                 # alt: 'local' (UUID-flat, dedup on, ~5x faster
                                 # for high-churn workloads), 's3' (multi-host).
MIRROR_VAULT_ROOT=               # blank → <home>/library
LOCAL_STORAGE_ROOT=              # blank → <home>/objects (only used by local)

WORKER_ENABLED=true              # default in embedded mode; TaskRunner runs in-process

LLM_DEFAULT_PROVIDER=openai      # or anthropic
LLM_DEFAULT_API_KEY=sk-...
LLM_DEFAULT_MODEL=gpt-4o-mini
# Per-profile overrides (chat / reflect / ingest / vision / audio):
LLM_REFLECT_MODEL=gpt-4o
LLM_VISION_MODEL=gpt-4o

# multi-machine mode only
MARGINALIA_SERVER=               # non-empty = remote mode, skip embedded
```

OpenAI-compatible endpoints (Together, Groq, DeepSeek, local vLLM /
ollama) are supported via `LLM_*_BASE_URL`.

## Deployment shape

**Default (embedded)**: `marginalia` mounts the FastAPI app + TaskRunner
in its own process. HTTP never hits a socket — `httpx.ASGITransport`
calls the ASGI app directly. This is the right shape for 99% of usage.

```
   ┌──────────────────────────────────────┐
   │  marginalia  (CLI + ASGI + worker)   │
   └──────────────────────────────────────┘
```

**Multi-machine** (optional): split the server into a standalone process
and have CLIs HTTP into it. SQLite only allows one writer process at a
time — use Postgres for multi-machine setups.

```
   ┌─────────────┐         ┌──────────────────┐
   │  marginalia │   HTTP  │  uvicorn server  │
   │     CLI     ├────────►│  marginalia.main │  (WORKER_ENABLED=true)
   └─────────────┘         └────────┬─────────┘
                                    │  shared Postgres + storage
                                    │
                            other clients connect to the same server
```

To run a remote server:

```bash
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
# On the client side:
marginalia --server http://server.lan:8000
# Or persist the choice in ~/.marginalia/.env:
MARGINALIA_SERVER=http://server.lan:8000
```

## Development

```bash
# run any single end-to-end test
.venv/Scripts/python tests/test_agent_e2e.py

# run all 30 e2e tests
for t in tests/test_*_e2e.py; do .venv/Scripts/python "$t"; done
```

30 e2e tests cover upload, ingest, reflect, dispatcher, purge,
normalize_tags, enrich_tags, lifecycle, restructure, agent runtime,
agent tools, user mgmt, CLI, image pipeline, user files, export, pdf,
pdf-with-images, pdf-OCR, duckdb tools, worker daemon, mine_corpus_evidence,
mine_session_cooccurrence, propose_views, refresh_entry_extra,
container, git repo, compression / archive, office (docx + spreadsheet),
and CLI upgrade (incl. embedded-mode smoke).

## Status

Marginalia is at v1: end-to-end functional but not yet hardened against
real-world data. Known gaps:

- Recommendation-style background mining (cooccurrence, random walk) is
  on the next-cycle list — `mine_session_cooccurrence` exists as a
  placeholder task but the scoring is shallow.
- No semantic / embedding retrieval. Recall is name + summary + tags +
  FTS5 against ingested text. Adequate for personal libraries; not
  intended to replace vector search if you need it.
- Audio / video files are accepted but have no pipeline. Speech-to-text
  is a future cycle.

## License

Copyright (c) 2026 shenmintao

Marginalia is licensed under the GNU Affero General Public License v3.0
or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for the full text.

If you run a modified version of Marginalia as a network service, the
AGPL requires you to make the corresponding source available to your
users.
