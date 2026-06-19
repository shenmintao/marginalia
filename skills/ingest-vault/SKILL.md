---
name: ingest-vault
description: Bulk-admit files from a Marginalia mirror vault into the database, then let the LLM pipeline catch up in the background. Use when the user has dropped a stack of PDFs / markdown / notes into their vault directory and wants them indexed and searchable.
compatibility: Requires the `marginalia` CLI (Python 3.11+), a configured LLM ingest profile, and STORAGE_BACKEND=mirror for bulk ingest. For STORAGE_BACKEND=local, use `marginalia upload` per file instead.
allowed-tools: bash read
---

# Ingest a vault into Marginalia

Marginalia is a personal knowledge base. The user keeps the canonical
files on disk (the "mirror vault") and Marginalia tracks each file with
a database entry plus AI-extracted metadata. This skill walks through
the bulk-ingest path: admit fast, run LLM extraction async.

## When to use

- The user dragged a folder of files into the vault and asks "index these".
- The user says "I just downloaded a bunch of papers, can you add them?"
- The user is migrating from another tool and wants files imported.

## Prerequisites

- The vault root is configured (`MARGINALIA_HOME` env or `marginalia init`).
- `STORAGE_BACKEND=mirror` (the default). For `local`, the user must use
  `/upload` per file instead — bulk ingest is mirror-only.
- Files are already in the vault directory. Bulk ingest does not COPY
  files in; it only registers what is already on disk.

## Workflow

1. **Start the REPL.** From the vault directory:

   ```
   marginalia
   ```

   The prompt looks like `marginalia[mirror />` once connected. The
   bracket shows backend + cwd + queue depth.

2. **See what's new on disk.** `/check` runs a scan and reports four
   categories:

   ```
   /check
   ```

   Output groups files into: `new` (on disk, not in db), `modified`
   (content changed), `moved` (folder/name changed), `missing` (in db,
   gone from disk). Read the counts before applying — surprising
   `missing` numbers often indicate the user is in the wrong directory.

3. **Apply everything.** This is the bulk-ingest entry point:

   ```
   /ingest --all
   ```

   It admits each new file (creates the db row, hashes the bytes), then
   queues an LLM extraction task per file. Progress bar shows N/M for
   admission. When admission finishes, the prompt's `N busy` count
   reflects the LLM queue.

4. **Let the queue drain in the background.** The user can keep working —
   ask questions, run searches — while ingestion completes. The prompt's
   `N busy` reading drops as tasks finish.

   If the user wants to wait explicitly, tell them: leave the REPL open;
   on exit, they'll be prompted "wait or quit". `q` is safe — the next
   launch resumes via `recover_stuck_tasks`.

## Targeted ingest

If the user only wants part of the vault (say, one new folder):

```
/ingest path/to/folder
/ingest single_file.pdf
```

These accept relative paths from cwd. Same admission + queue flow as
`--all`, just scoped.

## Common pitfalls

- **"Where's my file?"** Mirror mode requires the file to live UNDER the
  vault root. If the user pasted a path outside the vault, the CLI
  prints `→ /upload is for copying files INTO the vault.` and refuses.
  Direct them to either move the file into the vault or use `/upload`.

- **Storage backend mismatch.** If the user previously ran with
  `STORAGE_BACKEND=local` and switched, lifespan startup raises
  `StorageBackendMismatchError`. Tell them to run
  `marginalia storage migrate --from local --to mirror` (or revert).

- **Long queue, no apparent progress.** The `N busy` count reflects the
  task queue. If it's stuck above zero with no decrease over several
  minutes, the LLM provider may be unconfigured or throttled. Check
  `MARGINALIA_LLM_*` env settings.

## After ingest

Once `N busy` settles back near zero, the corpus is ready for:

- **search-by-question** → see `research-with-marginalia` skill
- **discovery / related-entries** → see `discover-and-curate` skill

## One-shot commands

All of the above can be driven non-interactively by an external agent:

```bash
marginalia check --json
marginalia ingest --all --yes --json
marginalia ingest path/to/folder --yes --json
marginalia background --json
marginalia reprocess failed --json
marginalia reprocess folder <full_folder_id> failed --json
marginalia upload ./somewhere/paper.pdf /papers/
```

Add `--json` for machine-parseable output. `--yes` skips confirmation prompts.
The CLI auto-discovers the backend like the REPL. IDs must be **full UUIDs**.
