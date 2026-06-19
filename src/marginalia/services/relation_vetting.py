"""On-demand relation vetting shared by discovery surfaces."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entry_relations as relations_repo

log = logging.getLogger(__name__)

ON_DEMAND_VET_LIMIT = 12
ON_DEMAND_MIN_OBSERVATION = 2


@dataclass(slots=True, frozen=True)
class OnDemandVetResult:
    candidates: int = 0
    accepted: int = 0
    rejected: int = 0
    failed: int = 0
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.accepted or self.rejected)


async def vet_direct_relations_for_entry(
    db: AsyncSession,
    *,
    entry_id: str,
    limit: int = ON_DEMAND_VET_LIMIT,
    min_observation: int = ON_DEMAND_MIN_OBSERVATION,
) -> OnDemandVetResult:
    """Vet strongest unjudged direct neighbours for `entry_id`.

    This is the lazy counterpart to the batch `vet_relations` task. It only
    touches direct edges that a `/discover` request is about to need, then
    caches the LLM verdict on `entry_relations` so the next request is free.
    Failures are logged and leave edges unvetted for a future attempt.
    """
    if not await relations_repo.has_direct_unvetted_candidate(
        db,
        entry_id=entry_id,
        min_obs=min_observation,
    ):
        return OnDemandVetResult()

    candidates = await relations_repo.list_direct_unvetted_candidates(
        db,
        entry_id=entry_id,
        min_obs=min_observation,
        limit=max(1, limit),
    )
    if not candidates:
        return OnDemandVetResult()

    try:
        from marginalia.tasks.handlers import vet_relations as vet_mod

        client = vet_mod.get_chat_client("ingest")
        verdicts, error = await vet_mod._ask_llm(client, candidates)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        log.warning("on-demand relation vetting failed: %s", error)
        return OnDemandVetResult(candidates=len(candidates), failed=len(candidates), error=error)

    if error is not None:
        return OnDemandVetResult(candidates=len(candidates), failed=len(candidates), error=error)

    verdict_by_id = {v["pair_id"]: v for v in verdicts}
    now = datetime.now(timezone.utc)
    accepted = 0
    rejected = 0
    failed = 0
    for cand in candidates:
        verdict = verdict_by_id.get(cand["pair_id"])
        if verdict is None:
            failed += 1
            continue
        yes = verdict["verdict"] == "yes"
        reason = (verdict.get("reason") or "").strip()[:500]
        await relations_repo.update_vetted(
            db,
            relation_id=cand["relation_id"],
            vetted=yes,
            vetted_reason=reason,
            vetted_at=now,
            vetted_observation_count=int(cand["observation_count"] or 0),
        )
        await audit_events_repo.append(
            db,
            kind="relation_vetted",
            payload={
                "relation_id": cand["relation_id"],
                "entry_a_id": cand["entry_a_id"],
                "entry_b_id": cand["entry_b_id"],
                "verdict": verdict["verdict"],
                "reason": reason,
                "observation_count": cand["observation_count"],
                "scheduled_by": "discover:on_demand",
            },
        )
        if yes:
            accepted += 1
        else:
            rejected += 1

    return OnDemandVetResult(
        candidates=len(candidates),
        accepted=accepted,
        rejected=rejected,
        failed=failed,
    )
