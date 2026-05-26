"""refresh_entry_extra — synthesize per-entry insight from accumulated journals.

Counterpart to reflect_turn's per-conversation `entry_extra_updates`:
where reflect produces position-aware insight from ONE conversation,
this task synthesizes across MANY journal notes that mention the same
entry over time. Result is written to file_entries.extra.

Conflict policy:
  reflect_turn and refresh_entry_extra both write file_entries.extra.
  Last writer wins — by design (cycle plan). reflect's insight is fresh
  but narrow (one conv); refresh's insight is integrative but lags.
  Future: a `source` field could distinguish, but V1 accepts this.

Inputs:
  payload (all optional):
    "window_days" int (14 default), "min_journals" int (3 default),
    "cap" int (20 default), "dry_run" bool.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage,
    ChatRequest,
    TextBlock,
    get_chat_client,
)
from marginalia.llm.tagged_response import parse_tagged
from marginalia.repositories import entries as entries_repo
from marginalia.repositories import entry_tags as entry_tags_repo
from marginalia.repositories import journal as journal_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_REFRESH_ENTRY_EXTRA, task_handler

log = logging.getLogger(__name__)

WINDOW_DAYS = 14
MIN_JOURNALS = 3
CAP = 20
SOURCE_KIND = "refresh_entry_extra"

REFRESH_SYSTEM = """You are Marginalia's per-entry insight synthesizer.

You receive ONE file_entry's metadata plus the journal notes (the
agent's reflective notebook) that have mentioned it recently. Produce
a single short paragraph (2-5 sentences) capturing what is CURRENTLY
worth remembering about this entry — the cross-cutting insight that
emerges from how it has been used.

Rules:
- The new `extra` should integrate the journals into a coherent view.
- Do NOT just list journals; synthesize.
- Do NOT speculate beyond what's in the journals + entry metadata.
- If the journals don't add up to anything meaningful, return the
  `current_extra` unchanged (signaling no refresh needed).

Output format — exactly one block:

  <extra>
  free-form prose, 2-5 sentences
  </extra>

Do NOT wrap in JSON, do NOT add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
REFRESH_SCHEMA: dict[str, Any] = {}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _hash(text: str | None) -> str:
    if text is None:
        return ""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]

@task_handler(KIND_REFRESH_ENTRY_EXTRA)
async def handle_refresh_entry_extra(payload: Mapping[str, Any]) -> None:
    window = int(payload.get("window_days") or WINDOW_DAYS)
    min_journals = int(payload.get("min_journals") or MIN_JOURNALS)
    cap = int(payload.get("cap") or CAP)
    dry_run = bool(payload.get("dry_run") or False)

    candidates = await _build_candidates(
        window_days=window,
        min_journals=min_journals,
        cap=cap,
    )
    if not candidates:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_REFRESH_ENTRY_EXTRA,
                object_kind=GLOBAL_OBJECT_KIND,
                object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"candidates": 0,
                        "reason": "no entry has enough recent journal mentions"},
            )
            await session.commit()
        return

    applied = 0
    noop_count = 0
    for cand in candidates:
        result = await _process_one(cand, dry_run=dry_run)
        if result == "applied":
            applied += 1
        else:
            noop_count += 1

    async with session_scope() as session:
        await record_outcome(
            session,
            task_kind=KIND_REFRESH_ENTRY_EXTRA,
            object_kind=GLOBAL_OBJECT_KIND,
            object_id=GLOBAL_OBJECT_ID,
            outcome="applied" if applied else "noop",
            detail={
                "candidates": len(candidates),
                "applied": applied,
                "noop": noop_count,
                "window_days": window,
                "min_journals": min_journals,
                "cap": cap,
                "dry_run": dry_run,
            },
        )
        await session.commit()

    log.info("refresh_entry_extra: candidates=%d applied=%d noop=%d",
             len(candidates), applied, noop_count)

async def _build_candidates(
    *,
    window_days: int,
    min_journals: int,
    cap: int,
) -> list[dict[str, Any]]:
    """Find entries with ≥ min_journals journal mentions in the window."""
    cutoff = _utcnow() - timedelta(days=window_days)
    async with session_scope() as session:
        rows = await journal_repo.list_id_entry_ids_note_created(
            session, cutoff=cutoff,
        )
        # entry_id → list of (journal_id, journal_note, journal_created_at)
        mentions: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for jid, entry_ids, note, created_at in rows:
            for eid in entry_ids:
                mentions[str(eid)].append({
                    "journal_id": jid,
                    "note": note,
                    "created_at": (
                        created_at.isoformat() if created_at else None
                    ),
                })

        eligible_ids = [
            eid for eid, lst in mentions.items() if len(lst) >= min_journals
        ]
        if not eligible_ids:
            await session.commit()
            return []

        # Pull entry + file metadata for each
        entry_rows = await entries_repo.list_active_with_file_by_ids(
            session, eligible_ids,
        )
        if not entry_rows:
            await session.commit()
            return []

        # Batch fetch tags for all eligible entries
        tag_rows = await entry_tags_repo.list_id_name_facet_for_entries(
            session, [e.id for e, _ in entry_rows],
        )
        tags_by_entry: dict[str, list[dict[str, str]]] = defaultdict(list)
        for eid, _tid, name, facet in tag_rows:
            tags_by_entry[eid].append({"name": name, "facet": facet})

        candidates: list[dict[str, Any]] = []
        # Order by mention-count descending (most-discussed first)
        ranked = sorted(
            entry_rows,
            key=lambda pair: len(mentions.get(pair[0].id, [])),
            reverse=True,
        )
        for entry, file_row in ranked[:cap]:
            candidates.append({
                "entry_id": entry.id,
                "display_name": entry.display_name,
                "file_summary": file_row.summary or "",
                "current_extra": entry.extra or "",
                "tags": tags_by_entry.get(entry.id, []),
                "journals": mentions[entry.id],
            })
        await session.commit()
    return candidates

async def _process_one(
    cand: dict[str, Any],
    *,
    dry_run: bool,
) -> str:
    """Returns 'applied' or 'noop'."""
    new_extra = await _ask_llm(cand)
    if new_extra is None:
        # parsing error — record as noop with reason
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_REFRESH_ENTRY_EXTRA,
                object_kind="file_entry",
                object_id=cand["entry_id"],
                outcome="noop",
                detail={
                    "reason": "llm_parse_failed",
                    "journal_count": len(cand["journals"]),
                },
            )
            await session.commit()
        return "noop"

    new_extra = new_extra.strip()
    old_extra = cand["current_extra"].strip()
    if new_extra == old_extra:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_REFRESH_ENTRY_EXTRA,
                object_kind="file_entry",
                object_id=cand["entry_id"],
                outcome="noop",
                detail={
                    "reason": "extra_unchanged",
                    "journal_count": len(cand["journals"]),
                },
            )
            await session.commit()
        return "noop"

    if dry_run:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_REFRESH_ENTRY_EXTRA,
                object_kind="file_entry",
                object_id=cand["entry_id"],
                outcome="noop",
                detail={
                    "reason": "dry_run",
                    "would_write": True,
                    "old_extra_hash": _hash(old_extra),
                    "new_extra_hash": _hash(new_extra),
                    "journal_count": len(cand["journals"]),
                },
            )
            await session.commit()
        return "noop"

    async with session_scope() as session:
        await entries_repo.update_extra(
            session, entry_id=cand["entry_id"], extra=new_extra, now=_utcnow(),
        )
        await audit_events_repo.append(
            session,
            kind="entry_extra_refreshed",
            payload={
                "entry_id": cand["entry_id"],
                "journal_count": len(cand["journals"]),
                "old_extra_hash": _hash(old_extra),
                "new_extra_hash": _hash(new_extra),
                "source_kind": SOURCE_KIND,
            },
        )
        await record_outcome(
            session,
            task_kind=KIND_REFRESH_ENTRY_EXTRA,
            object_kind="file_entry",
            object_id=cand["entry_id"],
            outcome="applied",
            detail={
                "journal_count": len(cand["journals"]),
                "old_extra_hash": _hash(old_extra),
                "new_extra_hash": _hash(new_extra),
            },
        )
        await session.commit()
    return "applied"

async def _ask_llm(cand: dict[str, Any]) -> str | None:
    user_payload = {
        "entry": {
            "entry_id": cand["entry_id"],
            "display_name": cand["display_name"],
            "file_summary": cand["file_summary"],
            "current_extra": cand["current_extra"],
            "tags": cand["tags"],
        },
        "journals": cand["journals"],
    }
    user_text = (
        "Synthesize this entry's `extra` from the journal mentions and "
        "current metadata. If nothing has changed meaningfully, return "
        "the `current_extra` unchanged.\n\n"
        f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>"
    )
    client = get_chat_client("ingest")
    resp = await client.complete(ChatRequest(
        system=REFRESH_SYSTEM,
        messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
        max_tokens=2048,
        temperature=0.2,
    ))
    tagged = parse_tagged(resp.text or "")
    extra = tagged.get("extra", "").strip()
    if not extra:
        log.warning("refresh_entry_extra: no <extra> in response for entry %s",
                    cand["entry_id"])
        return None
    return extra
