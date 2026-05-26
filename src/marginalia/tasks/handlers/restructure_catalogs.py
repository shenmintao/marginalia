"""restructure_catalogs — DESIGN.md §9.4 (重整书架分类).

LLM-driven catalog tree maintenance. The agent (offline librarian) is asked
to propose a small list of typed operations against the current catalog
tree, given:
  - current catalog tree with doc_count per node (entries currently linked)
  - recent journal entries that carry `tags=['hint:restructure_catalogs']`
  - high-activity active entries that may be misclassified

The LLM returns an ordered list of operations. The backend validates and
applies them in ONE transaction; any operation that fails validation is
recorded as `rejected` in task_outcomes but does NOT abort the others.

Operations supported (design §9.4 + §14.4):
  - `rename`        — change catalogs.name
  - `move`          — change catalogs.parent_id (cycle-checked)
  - `update_extra`  — overwrite catalogs.extra
  - `create`        — INSERT a new catalog (uses temp_id so subsequent ops
                      can reference it)
  - `soft_delete`   — set catalogs.deleted_at; if merge_into supplied,
                      reassign children + child-entries to target; otherwise
                      promote children to root (parent_id=NULL) and reassign
                      entries to NULL (uncategorised)
  - `move_entries`  — UPDATE file_entries.catalog_id for a list of entry_ids

Hard invariants:
  - NEVER hard-delete a catalog row (design §14.4 #2 — AI never deletes;
    delete is user-only). soft_delete sets deleted_at.
  - NEVER produce a parent cycle. After every `move`/`create`, walk the
    parent chain to verify.
  - alias_of-style chains do not exist on catalogs; parent_id is a normal
    tree edge. Soft-deleted catalogs are skipped from "current tree" the
    LLM sees but their rows remain for history.
  - file_entries.catalog_id pointing at a now-soft-deleted catalog is
    rewritten by soft_delete (atomic, same transaction).

Audit:
  - `catalog_moved` per move (parent_id change)
  - `catalog_updated` per rename / update_extra / create
  - `lifecycle_changed` is NOT emitted (this handler does not touch entry
    lifecycle)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Mapping

from marginalia.db.session import session_scope
from marginalia.llm import ChatMessage, ChatRequest, TextBlock, get_chat_client
from marginalia.llm.tagged_response import parse_tagged
from marginalia.repositories import catalogs as catalogs_repo
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_RESTRUCTURE_CATALOGS, task_handler
from marginalia.utils.ids import new_id

log = logging.getLogger(__name__)

JOURNAL_HINT_DAYS = 14            # window to fetch hint:restructure_catalogs notes
HIGH_ACTIVITY_TOP_N = 50          # top-N most-recently-touched active entries
MAX_OPERATIONS = 20               # LLM cap; protects against runaway output


RESTRUCTURE_SYSTEM = """You are Marginalia's catalog tree maintainer.

You are given:
  - the current catalog tree, with each node's doc_count (entries linked)
  - recent reflection notes hinting that some classification is off
  - a sample of currently-active entries with their tags + summary + extra

Decide a SMALL set of operations to improve the tree. Be conservative:
  - Most runs should change nothing (return an empty <operations> block).
  - Prefer renames over splits; prefer splits over deletes; prefer moves
    over rewrites.
  - Aim for at most 5 operations per run.
  - When creating a new catalog, you may reference it in subsequent
    operations via the temp_id you assign (e.g. "tmp_AI").

Operation types and their fields — emit ONE compact JSON object per line:
  rename:        {"op":"rename", "catalog_id":"...", "new_name":"..."}
  move:          {"op":"move", "catalog_id":"...", "new_parent_id":null}
  update_extra:  {"op":"update_extra", "catalog_id":"...", "extra":"..."}
  create:        {"op":"create", "temp_id":"...", "name":"...", "parent_id":null, "summary":"...", "description":"...", "tags":[]}
  soft_delete:   {"op":"soft_delete", "catalog_id":"...", "merge_into":null}
                 -- if merge_into is null, the deleted catalog's children
                    promote to root and its entries become uncategorised.
  move_entries:  {"op":"move_entries", "entry_ids":["..."], "target_catalog_id":"..."}
                 -- target may be a temp_id from an earlier create

NEVER hard-delete a catalog. NEVER create a parent cycle. NEVER touch any
entry lifecycle, files content, or tags table.

Output format — exactly one block, one operation per line:

  <operations>
  {"op":"rename", "catalog_id":"cat_123", "new_name":"Machine Learning"}
  {"op":"move", "catalog_id":"cat_456", "new_parent_id":"cat_123"}
  </operations>

Leave the block EMPTY if no changes are warranted. Do NOT wrap the whole
output in an outer JSON object and do NOT add ``` fences. Each line must
be a self-contained JSON object.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
RESTRUCTURE_SCHEMA: dict[str, Any] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@task_handler(KIND_RESTRUCTURE_CATALOGS)
async def handle_restructure_catalogs(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    snapshot = await _take_snapshot(now=now)

    if not snapshot["catalogs"]:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_RESTRUCTURE_CATALOGS,
                object_kind=GLOBAL_OBJECT_KIND,
                object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"reason": "no_catalogs_yet"},
            )
            await session.commit()
        return

    operations = await _ask_llm_for_operations(snapshot)
    operations = operations[:MAX_OPERATIONS]

    from marginalia.tasks.handlers.restructure_catalogs_apply import apply_operations
    await apply_operations(operations=operations, now=_utcnow())


# ----- snapshot --------------------------------------------------------------

async def _take_snapshot(*, now: datetime) -> dict[str, Any]:
    journal_cutoff = now - timedelta(days=JOURNAL_HINT_DAYS)
    async with session_scope() as session:
        catalogs = await catalogs_repo.list_all_live(session)

        # doc_count per catalog from file_entries (live entries only)
        counts = await catalogs_repo.direct_entry_counts(session)

        catalog_view = [
            {
                "id": c.id,
                "parent_id": c.parent_id,
                "name": c.name,
                "summary": c.summary,
                "description": c.description,
                "tags": c.tags,
                "extra": c.extra,
                "doc_count": counts.get(c.id, 0),
            }
            for c in catalogs
        ]

        hints = await journal_repo.list_recent_with_hints(
            session, cutoff=journal_cutoff, limit=40,
        )
        hint_view = [
            {"note": n, "entry_ids": list(e or []), "tags": list(t or []),
             "created_at": ca.isoformat()}
            for n, e, t, ca in hints
            if any(str(tag).startswith("hint:") for tag in (t or []))
        ]

        # high-activity entries: most recently updated active entries
        # (cheap proxy — restructure does not need a full query stack here)
        active_entries = await entries_repo.list_active_recent_updated(
            session, limit=HIGH_ACTIVITY_TOP_N,
        )

        entry_view = []
        if active_entries:
            tag_rows = await entry_tags_repo.list_id_name_facet_for_entries(
                session, [e.id for e in active_entries],
            )
            tags_by_entry: dict[str, list[dict[str, Any]]] = {}
            for eid, _tid, name, facet in tag_rows:
                tags_by_entry.setdefault(eid, []).append(
                    {"name": name, "facet": facet}
                )
            for e in active_entries:
                entry_view.append({
                    "entry_id": e.id,
                    "display_name": e.display_name,
                    "catalog_id": e.catalog_id,
                    "extra": e.extra,
                    "tags": tags_by_entry.get(e.id, []),
                })

        await session.commit()

    return {
        "catalogs": catalog_view,
        "hints": hint_view,
        "active_entries": entry_view,
    }


# ----- LLM -------------------------------------------------------------------

async def _ask_llm_for_operations(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    user_text = (
        "Decide what to change. Be conservative — most runs should output "
        "an empty operations block.\n\n"
        f"<snapshot>\n{json.dumps(snapshot, ensure_ascii=False)}\n</snapshot>"
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=RESTRUCTURE_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=4096,
        temperature=0.2,
    ))
    tagged = parse_tagged(resp.text or "")
    block = tagged.get("operations", "")
    ops: list[dict[str, Any]] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning("restructure_catalogs: skip malformed op line %r: %s",
                        line[:120], exc)
            continue
        if isinstance(obj, dict) and obj.get("op"):
            ops.append(obj)
    return ops
