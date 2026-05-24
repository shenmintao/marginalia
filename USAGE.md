# Marginalia Operations Manual

> 中文版:[USAGE.zh-CN.md](USAGE.zh-CN.md)
>
> Written for users who already know their way around a terminal. Install
> → run one complete flow → know where to look when things break.
>
> This manual does not explain *why* things are designed this way — that
> belongs in [`DESIGN.md`](DESIGN.md). The command list and CLI flags
> live in [`README.md`](README.md).

---

## 1. Install

Requires Python 3.11+.

```bash
git clone <repo>
cd marginalia
python -m venv .venv
source .venv/Scripts/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

You're set when `marginalia --help` prints the command list.

---

## 2. Initialize a library

A library = one directory holding the db + your files + caches.

```bash
mkdir my-library && cd my-library
marginalia init
```

`init` lays down, in the current directory:
- `.env` — config file; you'll need to fill in your API key
- `data/` — where the SQLite database lands
- `library/` — your uploaded files, stored as a real readable folder tree
- `.marginalia/` — caches

To put the library elsewhere, set `MARGINALIA_HOME`:

```bash
export MARGINALIA_HOME=/some/other/path
marginalia init
```

---

## 3. Configure the LLM (DeepSeek V4 Flash example)

Open `.env` and edit these lines:

```ini
LLM_DEFAULT_PROVIDER=openai
LLM_DEFAULT_BASE_URL=https://api.deepseek.com/v1
LLM_DEFAULT_API_KEY=sk-your-key
LLM_DEFAULT_MODEL=deepseek-v4-flash
```

DeepSeek is OpenAI-API-compatible, so `provider=openai` works — you just
swap `BASE_URL` and `MODEL`.

`LLM_DEFAULT_*` covers everything by default. Per-task overrides exist
(`LLM_REFLECT_MODEL`, `LLM_INGEST_MODEL`, etc.) for routing expensive
but low-frequency stages to a stronger model — say `deepseek-v4-pro` —
but `v4-flash` is smart enough that you usually don't need to bother.

Vision (image ingest) and audio (transcription) need their own config
because they typically use a different provider:

```ini
# Leave blank if you have no vision model — images will skip the VLM stage
LLM_VISION_BASE_URL=https://api.deepseek.com/v1
LLM_VISION_MODEL=deepseek-vl
```

After editing `.env`, run migrations once:

```bash
alembic upgrade head
```

---

## 4. Run one complete flow

```bash
marginalia
```

Drops you into the REPL. The five steps below are the minimum viable
flow:

```
marginalia> /upload paper.pdf /
   ↳ copies paper.pdf into the vault root, queues an ingest task

marginalia> /tree
   ↳ shows the folder structure — confirm paper.pdf landed

marginalia> /info <entry_id>
   ↳ shows ingest status. Only counts as ingested when status reaches
     pending → processing → done
   ↳ first run takes ten-ish seconds to a minute (depends on model speed)

marginalia> what is this paper about?
   ↳ enters the agent flow: planning → tool calls → answer
   ↳ the answer ends with [^a] [^b] footnotes citing specific passages

marginalia> /export
   ↳ packs that conversation (with cited excerpts) into a zip in cwd
```

`✓ answer ready` means the turn finished cleanly.

---

## 5. Reading the event stream

After you ask a question, lines stream in:

```
⠋ planning the investigation...      ← Plan phase (no tools, just thinking)
⠋ calling search_journal(...)         ← Execute phase calls a tool
⠋ calling read_files(entry_id=...)
⠋ investigator thinking...            ← LLM is writing the answer
✓ answer ready
```

**No `planning` line, jumps straight to `answer ready`**: normal. The
plan phase classified your input as "small talk" or "trivial lookup" and
short-circuited.

**`thinking` hangs for a while**: the LLM is generating the final
answer. Wait it out.

**Same `calling X(...)` repeats**: the agent is looping on the same
tool. The framework's doom-loop guard forces an end within 6 cycles. If
this keeps happening, see §8 troubleshooting.

**The trailing `[tokens in=X out=Y tools=N llm_calls=M ...]`**: cost
breakdown for the turn.

---

## 6. Uploading more files

### Single file

```
marginalia> /upload ~/Downloads/paper.pdf /papers
```

The second arg is a vault-internal path. Leading `/` = absolute (from
vault root); no leading `/` = relative to the current remote cwd.

### Switch remote cwd, then bulk upload

```
marginalia> /cd /papers/2024
marginalia> /upload ~/Downloads/p1.pdf
marginalia> /upload ~/Downloads/p2.pdf
   ↳ both land under /papers/2024/
```

### Copy a whole directory at once

```
marginalia> /upload ~/Downloads/notes /
   ↳ copies the entire notes/ directory into the vault root
```

### Name conflicts

```
marginalia> /on-conflict rename     # auto-suffix (1) (2)
marginalia> /on-conflict skip       # skip
marginalia> /on-conflict error      # raise (default)
```

Setting only applies to the current session.

### When you've edited files outside marginalia

```
marginalia> /check
   ↳ diffs disk vs db, read-only
marginalia> /ingest --all
   ↳ syncs changes into the db (think: git add -A)
```

Single-file sync:

```
marginalia> /ingest /papers/edited.md
```

---

## 7. How to ask questions

### One-shot Q&A (no REPL)

```bash
marg ask "Which of my saved diffusion-model papers cover score-based methods?"
```

Prints the answer to stdout, no session opened. Good for quick lookups.

### Open a chat session (multi-turn)

```bash
marg chat
   or in REPL: marginalia> /new
```

Each follow-up carries the prior context. End with `/clear` (marks the
session cleared) or `/quit`.

### Export a conversation

```bash
# single markdown file (with citation list)
marg export <conv_id> -o answer.md

# zip bundle (with cited source excerpts)
marg export <conv_id> --bundle -o report.zip

# every conversation in a session
marg export <session_id> --all --bundle -o session.zip
```

`<conv_id>` shows up in the trailing `[...]` line after each answer.
`$LAST` aliases the most recent turn.

---

## 8. Troubleshooting

### File stuck on `processing`

```
marginalia> /info <entry_id>
   ↳ check ingest_status. No movement for 5+ minutes = actually stuck
```

`recover_stuck_tasks` runs every 10 minutes and resets timed-out tasks
back to `pending`. Trigger a full maintenance cycle by hand:

```
marginalia> /tend
```

### Ingest fails repeatedly

```
marginalia> /info <entry_id>
   ↳ check last_error. The prefix tells you which stage: Parse: / LLM: / Route:
```

- `Parse:` — file parsing failed. Likely corrupt or unsupported format.
- `LLM:` — LLM call failed. 99% of the time it's API key / quota / network.
- `Route:` — auto-cataloging failed. Rare; usually the LLM emitted
  schema-invalid output. The next recover cycle will retry.

### LLM API key is dead

Tasks will fail until they go `dead`. Fix `.env`, restart marginalia,
and `recover_stuck_tasks` gives dead tasks one more shot.

### Force-reingest a file

No dedicated command. Most direct route:

```
marginalia> /info <entry_id>
   ↳ note the file_id
```

Then via API or SQL, set the corresponding `files` row's `ingested_at`
to NULL and `ingest_status` back to `pending`. The next recover cycle
re-queues it.

> Heads up: this bypasses the write-once contract. Use only when ingest
> has a real bug and needs a do-over. Not part of normal use.

---

## 9. Backup and migration

### Single-machine backup (SQLite + local / mirror storage)

`cp -r` the entire `MARGINALIA_HOME` directory. The db file, library,
and objects are all under it.

You can copy while marginalia runs, but `/quit` first is safer.

### Migrate SQLite → Postgres

1. Cleanly stop the SQLite library (`/quit`)
2. Stand up Postgres, edit `.env`:

```ini
DB_BACKEND=postgres
POSTGRES_DSN=postgresql+asyncpg://user:pass@localhost:5432/marginalia
```

3. Run migrations:

```bash
alembic upgrade head
```

4. Data migration is not yet automated — v1 assumes the library is
   small enough that "re-ingest from scratch" is acceptable. To preserve
   history, write your own dump-from-SQLite, INSERT-into-Postgres script.

### Migrate mirror / local → S3

```bash
marginalia storage migrate --to s3
```

Rewrites every `files.storage_key` to point at S3, bulk-PUTs the
physical files. S3 config in `.env` must already be filled in before
running this.

---

## 10. Sharing one library across machines

SQLite does not support multi-process writes — multi-machine requires
Postgres + S3.

**Machine A (server)**:

```bash
# .env: DB_BACKEND=postgres, STORAGE_BACKEND=s3, WORKER_ENABLED=true
uvicorn marginalia.main:app --host 0.0.0.0 --port 8000
```

**Machine B (client)**:

```bash
marginalia --server http://A.lan:8000
```

Or write `MARGINALIA_SERVER=http://A.lan:8000` into machine B's
`~/.marginalia/.env`, then plain `marginalia` works.

### Docker compose, full stack in one shot

```bash
echo "LLM_DEFAULT_API_KEY=sk-..." > .env
docker compose up -d
```

Brings up api + worker + Postgres + MinIO. `alembic upgrade head` runs
on api startup; the MinIO bucket is created by a one-shot init container.

```bash
marginalia --server http://localhost:8000
```

---

## 11. Watching what the background is doing

```
marginalia> /tend
   ↳ kicks off one full offline maintenance cycle (normalize_tags /
     enrich_tags / restructure / suggest_demotion etc.) and prints
     each task's output as it runs
```

Demoted / archived entries are excluded from new query recall by
default. To see what got auto-demoted:

```
marginalia> /search <keyword>
   ↳ defaults to active only. Add --include-archived to see everything
```

Restore an auto-archived entry:

```
marginalia> /restore <entry_id>
```

---

## 12. Quitting

```
marginalia> /quit
```

Or just Ctrl+D. In embedded mode this stops the server + worker too.
Tasks mid-flight are resumed by `recover_stuck_tasks` on next launch.
