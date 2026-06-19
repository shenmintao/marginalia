---
name: research-with-marginalia
description: Ask Marginalia a research question, follow the citations it returns, and export the result as a single markdown file or a self-contained zip. Use when the user wants to think with their corpus rather than just look up a file.
compatibility: Requires the `marginalia` CLI (Python 3.11+), a configured LLM chat profile, and pre-ingested files.
allowed-tools: bash read write
---

# Research with Marginalia

Marginalia answers questions in the style of a research assistant: it
plans, picks tools, reads cited entries, and returns an answer with
markdown footnotes pointing back to the source files. This skill covers
the full conversation loop including export.

## When to use

- The user asks a question that needs synthesis across multiple files
  ("how do X and Y differ", "what does my corpus say about Z").
- The user wants a citation-grounded answer they can paste elsewhere.
- The user wants to share or archive a finished conversation.

## Prerequisites

- Files are already ingested. If `/check` shows `new` items, point the
  user at `ingest-vault` first.
- An LLM profile is configured (`MARGINALIA_LLM_CHAT_*`). Without it,
  the agent can't run.

## Workflow

1. **Ask the question.** Just type it at the prompt — no prefix:

   ```
   how does my corpus describe consensus protocols?
   ```

   The agent streams events: planning, thinking, tool calls (search,
   open_entry, etc.), and finally the answer. The answer contains
   `[^a]`-style markers and a footnote block at the bottom.

2. **Follow a citation.** The footnote block lists `entry_id=...`. To
   inspect any cited entry:

   ```
   /info <entry_id_prefix>
   ```

   8 chars of the id are enough — tab completion (after the first /info
   in this session) suggests entries the user has already seen.

   `/info` shows display name, folder path, summary, and a section
   preview so the user can judge relevance without downloading the
   bytes.

3. **Pull the bytes if needed.** When the user wants the actual file:

   ```
   /download <entry_id> [<dest>]
   ```

   Defaults to cwd with the original filename. Folder ids work too
   (returns a zip of the folder's contents).

4. **Continue the conversation.** Subsequent questions in the same
   session reuse the agent state — follow-ups stay coherent. Type
   `/new` to start fresh when switching topics.

5. **Export the result.** Two formats:

   ```
   /export                                    # zip, default cwd
   /export <conv_id> notes/raft-vs-paxos.md  # single markdown
   /export <conv_id> archive.zip              # zip with refs
   ```

   - **`.md`** = self-contained markdown. Each footnote is rewritten
     from `entry_id=01abcd...` to `**display name** — folder/path/`
     plus the summary. Drop into Obsidian / Notion / any markdown tool.
   - **`.zip`** = full archive. `report.md` (the agent's answer
     verbatim), `manifest.json` (citation metadata), and a
     `references/` folder with the cited file bytes + per-entry
     metadata. Use when archiving or sharing the underlying sources.

   Omit conv_id and `/export` resolves to the most recent conversation
   from the local session, falling back to the server's most-recent.

## Reading the prompt

The bracketed prompt encodes live state:

- `marginalia> `              — minimal: no backend yet / no cwd state
- `marginalia[mirror /research]> ` — backend + remote cwd
- `marginalia[... 12 busy]> `  — 12 tasks in the queue (ingest, mining,
                                  reflection)

When `N busy` is high, search results may be stale (recently-ingested
files might still be extracting). It's fine to wait or proceed; just
flag it to the user if their question depends on something just added.

## Common pitfalls

- **Empty / shallow answer.** Often means search didn't surface
  enough. Try: rephrase the question, or pre-warm with `/search <terms>`
  to confirm there's actually content matching the topic.

- **Citation marked `(reference removed)` in the export.** The cited
  entry has been soft-deleted between turn-time and export-time. The
  conversation still exports; just one footnote degrades gracefully.

- **`/export` with no args fails with "no ended conversation".** A
  conversation only counts as "ended" once the agent has finished a
  turn. If the user just asked a question and immediately ran /export,
  wait for the answer to fully stream.

## Removing entries

There is no `/delete` command. This is intentional — AI does not mutate
user files directly. To remove an entry:

1. Delete the file from the mirror vault.
2. Run `/ingest --all`. Marginalia detects the missing file and
   soft-deletes the entry (sets `deleted_at`).

The `lifecycle` field (active / demoted / archived) can demote entries
out of default search results without deleting them. Set
`AUTO_LIFECYCLE_ENABLED=true` for automatic lifecycle suggestions.

## One-shot commands

All of the above can be driven non-interactively by an external agent:

```bash
marginalia ask "compare this Raft paper with my Paxos notes"
marginalia ask "..." --mode quick
marginalia search "consensus protocols" --json
marginalia info <full_entry_id> --json
marginalia discover <full_entry_id> --json
marginalia download <entry_id> [dest]
marginalia export <full_conv_id> [dest.md|dest.zip]
marginalia check --json
marginalia background --json
```

Add `--json` for machine-parseable output. Omit it for human-readable text.
`--server URL` (or `MARGINALIA_SERVER`) connects to a remote backend;
otherwise the CLI auto-discovers a running `marginalia serve` / desktop
sidecar, or starts an embedded backend.

One-shot CLI requires **full UUIDs** (e.g. `marginalia info b123a833-...`).
The 8-char prefix shorthand described in the REPL workflow above does not
work outside the REPL.

`marginalia export` requires a persistent backend (`marginalia serve` or
desktop). Two separate embedded-mode invocations (e.g. `marginalia ask` then
`marginalia export`) do not share state — the first process exits and its
conversations are gone. Export works inside the REPL because the embedded
backend stays alive across turns. When passing a conversation id, use the
**full UUID** — prefix shorthand does not work in one-shot mode.
