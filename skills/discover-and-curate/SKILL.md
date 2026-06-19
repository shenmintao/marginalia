---
name: discover-and-curate
description: Find related entries to a seed file, build reading lists, and surface neighbour clusters Marginalia has discovered automatically. Use when the user is browsing rather than asking a specific question.
compatibility: Requires the `marginalia` CLI (Python 3.11+), a configured LLM profile for relation vetting, and pre-ingested files. Relation mining needs at least one maintenance pass to produce results; new corpora have sparse relations initially.
allowed-tools: bash read write
---

# Discover and curate

Marginalia runs background "tend" passes that mine the corpus for
relations: tag overlap, citation graph, semantic neighbours. This skill
explains how to surface those relations from the CLI when the user
wants to explore rather than search.

## When to use

- The user has one file in mind and asks "what else is like this?"
- The user wants to build a reading list around a topic.
- The user asks "what is Marginalia learning about my corpus?"

## Prerequisites

- Ingestion has settled (`N busy` near zero in the prompt). Discovery
  works against files that already have summaries + tags + sections.
- A few "tend" cycles have run. New corpora have sparse relations until
  the miners have had a chance to walk the graph.

## Workflow

### 1. Find a seed entry

Either via search:

```
/search consensus protocols
```

Or by remembering an entry_id from a prior `/info` / `/discover` (tab
completion suggests prefixes once they're in this session's cache).

### 2. Discover related entries

```
/discover <entry_id>
```

Output: scored neighbours with a bar chart, sorted by relevance. A `*`
in the leading column flags a direct edge (citation, explicit relation)
versus a random-walk-derived neighbour.

```
/discover <entry_id> --all
```

By default discovery only returns relations the LLM has vetted. Pass
`--all` when the user wants the raw mining output too — useful for
spotting clusters that haven't been quality-gated yet.

### 3. Drill in

For each neighbour the user finds interesting:

```
/info <neighbour_entry_id>
```

The `summary` + section preview are usually enough to decide whether
to read the full file. If yes:

```
/download <neighbour_entry_id>
```

### 4. Trigger a fresh mining pass (optional)

If the user just ingested a lot of new files and wants the relation
graph updated immediately:

```
/tend
```

This kicks off a maintenance run: mining, vetting, normalization.
Returns a `tend_run_id` and a list of queued tasks. Watch the prompt's
`N busy` count to see when it settles.

```
/tend <tend_run_id>
```

Reports the status of that specific run.

## Curation patterns

### Reading list around a paper

1. `/discover <seed_id>` — get top-K neighbours.
2. For each that looks promising: `/info <neighbour>`. Read summary.
3. Note the entry_ids that pass muster. (Tab completion remembers them
   for the rest of the session.)
4. `/download <id>` for each, or zip the parent folder if they all live
   under one tree: `/download <folder_id> reading-list.zip`.

### Mapping a topic

1. `/search <broad term>` — surfaces top matches.
2. Pick the most central-looking result as a seed.
3. `/discover <seed>` — branch out one layer.
4. From the discovered set, pick a second seed in a different cluster.
5. Compare the two `/discover` outputs. Files that show up in both are
   genuine bridges between the clusters.

## Common pitfalls

- **Empty discovery results on a new corpus.** Mining miners haven't
  run yet. Either wait for the periodic tick, or run `/tend` once.

- **All neighbours are direct edges.** The seed is poorly indexed
  (short summary, missing tags) so random-walk can't find paths.
  Re-ingesting (`/ingest <path>`) re-runs extraction with the current
  pipeline, which usually fills these in.

- **Repeated noise in unvetted results.** That's why vetting exists.
  Drop `--all` and let the LLM filter.

## One-shot commands

All of the above can be driven non-interactively by an external agent:

```bash
marginalia search "consensus protocols" --json
marginalia info <full_entry_id> --json
marginalia discover <full_entry_id> --json
marginalia discover <full_entry_id> --top-k 12 --json
marginalia download <entry_id> [dest]
marginalia tend
marginalia background --json
```

Add `--json` for machine-parseable output. The CLI auto-discovers the backend
like the REPL. One-shot CLI requires **full UUIDs** — the 8-char prefix
shorthand from REPL mode does not work.
