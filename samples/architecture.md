# Marginalia Architecture Overview

This document is a developer-facing architecture sketch. It complements the full design in `DESIGN.md`.

## 1. System Shape

```text
CLI / desktop / HTTP client
        |
        v
FastAPI app (`marginalia.main`)
        |
        +-- synchronous request handlers
        |     upload, folders, entries, search, chat, export, settings
        |
        +-- TaskRunner
              ingest, reflect, tag quality, relation mining,
              catalog maintenance, pruning, lifecycle suggestions
```

Default mode embeds everything in one process:

```text
marginalia
  -> CLI REPL
  -> httpx ASGITransport
  -> FastAPI app
  -> in-process TaskRunner
```

Remote mode runs the API separately:

```text
marginalia --server http://host:8000
  -> HTTP
  -> uvicorn marginalia.main:app
```

## 2. Layers

```text
User-visible layer
  folders
  file_entries
  files

AI-internal retrieval layer
  catalogs
  views
  tags
  tag_aliases
  entry_tags
  entry_relations
  journal

Session and audit layer
  sessions
  conversations
  audit_events

Infrastructure layer
  tasks
  task_outcomes
```

Important separation:

- `files` describe immutable bytes.
- `file_entries` describe a file placement in the user's library.
- `journal` is the agent's persistent investigation memory.
- `entry_relations` is the evidence-discovery graph.
- `audit_events` is operational history, not retrieval memory.

## 3. Request Paths

### Upload and Ingest

```text
POST /v1/upload
  -> store bytes in mirror/local/s3
  -> create/reuse files row
  -> create file_entries row
  -> enqueue ingest_file

ingest_file
  -> resolve pipeline
  -> extract text/metadata/sections
  -> call ingest LLM profile where needed
  -> write files.summary / description / extra / kind
  -> assign catalog and tags
```

Pipelines:

```text
text
pdf
image
docx
spreadsheet
log
archive
```

### Chat Turn

```text
POST /v1/chat/{session_id}
  -> create conversation
  -> build stable snapshot
  -> plan LLM call
  -> execute LLM loop with tools
  -> stream SSE events
  -> persist answer and metrics
  -> enqueue reflect_turn
```

The execute loop can call:

```text
search_journal
list_folder
list_catalogs
read_catalog
resolve_tag
materialize_view
search_metadata
read_entries_metadata
read_files
query_log
query_sql
analyze_container
generate_chart
```

### Reflection

```text
reflect_turn
  -> replay same stable prefix shape as execute
  -> append compact current-turn summary
  -> ask reflect profile for one <entry> block
  -> insert journal row
```

The journal row is what future turns search. Raw conversations are persisted for audit/export, not used as primary retrieval memory.

## 4. Retrieval Funnel

```text
journal recall
  -> structured metadata filters
  -> catalog/tag/view/folder narrowing
  -> related entry graph
  -> original source read
  -> cited answer
```

The funnel intentionally delays expensive raw-file reads until candidates are plausible.

`read_files` provides targeted source access:

- text sections, headings, line ranges, regex matches;
- PDF physical page windows, page labels, regex matches;
- DOCX paragraph ranges;
- archive member paths;
- bounded offsets for long documents.

## 5. Evidence Graph

Background relation discovery turns usage and structure into retrieval hints.

```text
mine_relations
  -> session co-occurrence
  -> tag overlap
  -> citation co-citation
  -> corpus evidence candidates
  -> entry_relations

vet_relations
  -> LLM gate
  -> vetted=True/False

services.recommend.find_related
  -> random walk with restart
  -> related entries in search/metadata/discover
```

The online agent does not need to understand all mining signals. It receives related entries as compact candidates.

## 6. Long Document Strategy

Long files are never assumed to fit in one prompt.

Text:

- normal reads cap bytes according to requested window;
- deep reads can scan more for heading, line, section, or pattern lookup.

PDF:

- ingest can chunk or partially index long documents;
- readback extracts requested page windows;
- default reads return continuation hints;
- page labels are supported, but physical pages are the stable viewer locator;
- citation display tries quote location first.

## 7. Task Scheduling

The task queue is database-backed. No broker is required.

Important mechanics:

- priority controls claim order;
- leases and heartbeats recover crashed workers;
- active `dedup_key` uniqueness avoids duplicate background work;
- `task_outcomes` records idempotent effects and periodic recency.

Task families:

```text
online-adjacent: reflect_turn, ingest_file
self-healing:    recover_stuck_tasks
maintenance:     tag_quality, restructure_catalogs, suggest_lifecycle
discovery:       mine_relations, vet_relations, propose_views, refresh_entry_extra
retention:       purge_deleted_files, prune
dispatcher:      periodic_tick
```

## 8. Storage

Backends:

```text
mirror  readable folder tree under <home>/library
local   UUID object pool under <home>/objects
s3      remote object storage
```

Startup checks whether existing `storage_key` shapes match the configured backend. If not, the operator must migrate or restore the previous backend.

## 9. Deployment Choices

Personal library:

```text
SQLite + mirror + embedded CLI
```

High-churn local library:

```text
SQLite + local + embedded CLI
```

Shared or multi-host library:

```text
Postgres + S3 + API server + worker
```

SQLite is appropriate for one writer process. Use Postgres when multiple processes or machines can write.

## 10. Design Boundaries

Marginalia is not a vector search engine, a chat memory database, or a document summarizer that treats summaries as final evidence.

The intended contract is:

```text
structured narrowing
  + durable investigation memory
  + evidence graph
  + original-source verification
  = trustworthy private-library retrieval
```
