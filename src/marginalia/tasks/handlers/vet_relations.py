"""vet_relations — LLM gates the recommendation graph.

The miners (cooccurrence / tag_overlap / citation_graph) emit raw
candidate edges from statistics. Statistics are noisy:
  - tag overlap: two entries can share 3 generic tags but be totally
    unrelated content
  - cooccurrence: a single chat session that flits between topics will
    glue unrelated entries together
  - citation: an answer that makes a tangential reference will glue X
    to Y even if X is the answer's spine and Y is just an aside

This task is the gate. For each pair the LLM looks at both summaries
+ tag profiles + the signals that produced the edge and decides
yes/no. Only vetted=True edges are visible to the rest of the system
(find_related, the related_entries pre-fill in search/get_metadata).

Refresh policy: a vetted edge is reconsidered when the underlying
observation_count grows substantially past the snapshot taken at vet
time, or after VET_TTL_DAYS regardless. Fresh edges (vetted IS NULL)
are always in the candidate pool. Rejected edges (vetted=False) sit
quiet until their count grows past the refresh threshold.

Boundary rules:
  - Skip edges where either entry is soft-deleted or has no summary
    (can't ask the LLM to judge content it can't see).
  - Cap at MAX_VETS_PER_RUN per invocation; remaining edges wait for
    next /tend cycle.
  - Batch BATCH_SIZE pairs per LLM call to amortise prompt cost.

Writes:
  - entry_relations: vetted, vetted_reason, vetted_at,
    vetted_observation_count
  - audit_events: 'relation_vetted' per row
  - task_outcomes: per-pair detail + global summary
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.db.session import session_scope
from marginalia.llm import (
    ChatMessage, ChatRequest, TextBlock, get_chat_client,
)
from marginalia.llm.tagged_response import parse_kv, parse_tagged
from marginalia.repositories import entry_relations as relations_repo
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import KIND_VET_RELATIONS, task_handler

log = logging.getLogger(__name__)

MAX_VETS_PER_RUN = 200
BATCH_SIZE = 10
MIN_OBSERVATION_TO_VET = 2  # don't waste LLM tokens on 1-time blips
VET_TTL_DAYS = 180
REFRESH_GROWTH_FACTOR = 2  # current_count >= 2 * snapshot + buffer
REFRESH_GROWTH_BUFFER = 5

VET_RELATIONS_SYSTEM = """You are Marginalia's relation gatekeeper.

The system mined candidate links between entries from statistics
(co-citation, tag overlap, journal cooccurrence). For each candidate,
decide whether the two entries are genuinely related from a content
perspective — would a researcher reading one likely care about the
other?

Be conservative. Reject:
  - Coincidental overlaps (both happen to share a generic tag like
    "english" or "2024" but cover different topics).
  - Tangential mentions ("X cites Y in passing" is not Y being about
    the same thing as X).
  - Pairs whose summaries describe unrelated subject matter even if
    the signals are strong.
Accept:
  - Pairs whose summaries describe the same subject, the same domain,
    or one substantively builds on the other.

Output format — exactly one `<verdicts>` block, one line per candidate:

  <verdicts>
  <pair_id>: yes - one short sentence why
  <pair_id>: no - one short sentence why
  </verdicts>

Use the pair_id values verbatim from the input. The verdict MUST be
either `yes` or `no`. Separate verdict and reason with ` - ` (space,
dash, space). Do NOT wrap in JSON or add ``` fences.
"""


# Schema kept for legacy callers but no longer fed to the LLM.
VET_RELATIONS_SCHEMA: dict[str, Any] = {}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

def _ensure_aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes even on timezone=True columns; we
    treat them as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

@task_handler(KIND_VET_RELATIONS)
async def handle_vet_relations(payload: Mapping[str, Any]) -> None:
    cap = int(payload.get("cap") or MAX_VETS_PER_RUN)
    batch = int(payload.get("batch") or BATCH_SIZE)
    min_obs = int(payload.get("min_observation") or MIN_OBSERVATION_TO_VET)
    now = _utcnow()
    ttl_cutoff = now - timedelta(days=int(payload.get("ttl_days") or VET_TTL_DAYS))

    candidates = await _fetch_candidates(
        cap=cap,
        min_obs=min_obs,
        ttl_cutoff=ttl_cutoff,
    )
    if not candidates:
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_VET_RELATIONS,
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="noop",
                detail={"candidates": 0},
            )
            await session.commit()
        return

    yes_count = 0
    no_count = 0
    failed_count = 0
    last_error: str | None = None
    client = get_chat_client("ingest")

    # Process in batches to amortise LLM overhead.
    for chunk_start in range(0, len(candidates), batch):
        chunk = candidates[chunk_start: chunk_start + batch]
        verdicts, err = await _ask_llm(client, chunk)
        if err is not None:
            last_error = err
        if not verdicts:
            failed_count += len(chunk)
            continue
        verdict_by_id = {v["pair_id"]: v for v in verdicts}
        async with session_scope() as session:
            for cand in chunk:
                v = verdict_by_id.get(cand["pair_id"])
                if v is None:
                    failed_count += 1
                    continue
                yes = v["verdict"] == "yes"
                reason = (v.get("reason") or "").strip()[:500]
                await relations_repo.update_vetted(
                    session,
                    relation_id=cand["relation_id"],
                    vetted=yes,
                    vetted_reason=reason,
                    vetted_at=now,
                    vetted_observation_count=cand["observation_count"],
                )
                await audit_events_repo.append(
                    session,
                    kind="relation_vetted",
                    payload={
                        "relation_id": cand["relation_id"],
                        "entry_a_id": cand["entry_a_id"],
                        "entry_b_id": cand["entry_b_id"],
                        "verdict": v["verdict"],
                        "reason": reason,
                        "observation_count": cand["observation_count"],
                    },
                )
                if yes:
                    yes_count += 1
                else:
                    no_count += 1
            await session.commit()

    # Total failure: nothing was vetted. Raise so the task is requeued and
    # the failure surfaces in `task_outcomes` / `tasks.last_error` instead of
    # being silently absorbed as a `noop`.
    if yes_count == 0 and no_count == 0 and failed_count > 0:
        msg = (
            f"vet_relations: LLM returned no verdicts for any of "
            f"{failed_count} candidates"
        )
        if last_error:
            msg += f" (last error: {last_error})"
        async with session_scope() as session:
            await record_outcome(
                session,
                task_kind=KIND_VET_RELATIONS,
                object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
                outcome="error",
                detail={
                    "candidates": len(candidates),
                    "failed": failed_count,
                    "last_error": last_error,
                },
            )
            await session.commit()
        raise RuntimeError(msg)

    # Partial failure (some pairs vetted, some not): record so the user can
    # see degraded quality, but don't raise — committed work is real.
    outcome: Literal["applied", "noop", "error"]
    if failed_count > 0:
        outcome = "error"
    elif yes_count or no_count:
        outcome = "applied"
    else:
        outcome = "noop"

    async with session_scope() as session:
        await record_outcome(
            session,
            task_kind=KIND_VET_RELATIONS,
            object_kind=GLOBAL_OBJECT_KIND, object_id=GLOBAL_OBJECT_ID,
            outcome=outcome,
            detail={
                "candidates": len(candidates),
                "yes": yes_count,
                "no": no_count,
                "failed": failed_count,
                "last_error": last_error,
                "cap": cap,
                "batch": batch,
            },
        )
        await session.commit()

    log.info(
        "vet_relations: candidates=%d yes=%d no=%d failed=%d",
        len(candidates), yes_count, no_count, failed_count,
    )

async def _fetch_candidates(
    *, cap: int, min_obs: int, ttl_cutoff: datetime,
) -> list[dict[str, Any]]:
    """Pull up to `cap` relations needing vetting.

    Selection conditions (any one):
      1. Never vetted (vetted IS NULL).
      2. Vetted_at older than TTL.
      3. observation_count grew substantially past the snapshot
         (current >= REFRESH_GROWTH_FACTOR * snapshot + REFRESH_GROWTH_BUFFER).

    Joins both endpoints' file rows for summary + tags content.
    """
    async with session_scope() as session:
        rows = await relations_repo.list_vet_candidates(
            session, min_obs=min_obs,
        )

        out: list[dict[str, Any]] = []
        for r in rows:
            obs = r["observation_count"]
            vetted = r["vetted"]
            vetted_at = r["vetted_at"]
            vetted_obs = r["vetted_observation_count"]
            should_vet = False
            if vetted is None:
                should_vet = True
            elif vetted_at is not None and _ensure_aware(vetted_at) < ttl_cutoff:
                should_vet = True
            elif vetted_obs is not None and obs >= (
                REFRESH_GROWTH_FACTOR * vetted_obs + REFRESH_GROWTH_BUFFER
            ):
                should_vet = True
            if not should_vet:
                continue
            out.append({
                "pair_id": r["id"],
                "relation_id": r["id"],
                "entry_a_id": r["entry_a_id"],
                "entry_b_id": r["entry_b_id"],
                "observation_count": obs,
                "source_kind": r["source_kind"],
                "note": r["note"],
                "a_name": r["a_name"],
                "b_name": r["b_name"],
                "a_summary": r["a_summary"],
                "b_summary": r["b_summary"],
                "a_kind": r["a_kind"],
                "b_kind": r["b_kind"],
            })
            if len(out) >= cap:
                break
        return out

async def _ask_llm(
    client, chunk: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], str | None]:
    """Returns (verdicts, error_message). On success error_message is None;
    on failure verdicts is [] and error_message describes what went wrong so
    the caller can surface it via record_outcome / task.last_error."""
    payload = {
        "candidates": [
            {
                "pair_id": c["pair_id"],
                "signal": {
                    "source_kind": c["source_kind"],
                    "observation_count": c["observation_count"],
                    "note": c["note"][:200],
                },
                "a": {
                    "display_name": c["a_name"],
                    "kind": c["a_kind"],
                    "summary": (c["a_summary"] or "")[:600],
                },
                "b": {
                    "display_name": c["b_name"],
                    "kind": c["b_kind"],
                    "summary": (c["b_summary"] or "")[:600],
                },
            }
            for c in chunk
        ],
    }
    user_text = (
        "Decide for each candidate whether the two entries are "
        "genuinely related.\n\n"
        f"<candidates>\n{json.dumps(payload, ensure_ascii=False)}\n</candidates>"
    )
    try:
        resp = await client.complete(ChatRequest(
            system=VET_RELATIONS_SYSTEM,
            messages=[ChatMessage(role="user", content=[TextBlock(text=user_text)])],
            max_tokens=4096,
            temperature=0.1,
        ))
    except Exception as exc:  # noqa: BLE001
        log.warning("vet_relations: LLM call failed: %s", exc)
        return [], f"{type(exc).__name__}: {exc}"
    tagged = parse_tagged(resp.text or "")
    block = tagged.get("verdicts", "")
    if not block:
        log.warning("vet_relations: no <verdicts> block in response")
        return [], "no <verdicts> block in response"
    kv = parse_kv(block)
    out: list[dict[str, Any]] = []
    for pair_id, value in kv.items():
        verdict, sep, reason = value.partition(" - ")
        verdict = verdict.strip().lower()
        if verdict not in ("yes", "no"):
            continue
        out.append({
            "pair_id": pair_id,
            "verdict": verdict,
            "reason": reason.strip() if sep else "",
        })
    return out, None
