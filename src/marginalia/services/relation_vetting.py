"""Background relation-vetting scheduling shared by discovery surfaces."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.repositories import entry_relations as relations_repo
from marginalia.tasks.enqueue import enqueue
from marginalia.tasks.kinds import KIND_VET_RELATIONS

DIRECT_VET_LIMIT = 12
DIRECT_VET_MIN_OBSERVATION = 2
DISCOVER_VET_PRIORITY = 120


@dataclass(slots=True, frozen=True)
class DirectVetScheduleResult:
    requested: bool = False
    candidates_available: bool = False
    task_id: str | None = None
    dedup_key: str | None = None

    @property
    def queued(self) -> bool:
        return self.task_id is not None


async def schedule_direct_relation_vetting(
    db: AsyncSession,
    *,
    entry_id: str,
    limit: int = DIRECT_VET_LIMIT,
    min_observation: int = DIRECT_VET_MIN_OBSERVATION,
) -> DirectVetScheduleResult:
    """Queue background vetting for the seed's direct unvetted neighbours.

    `/discover?vet=true` uses this instead of doing LLM work inline. The
    caller gets a pure-read discovery response immediately; the queued task
    warms the vetted graph for the next request.
    """
    dedup_key = f"vet_relations:entry:{entry_id}"
    if not await relations_repo.has_direct_unvetted_candidate(
        db,
        entry_id=entry_id,
        min_obs=min_observation,
    ):
        return DirectVetScheduleResult(
            requested=True,
            candidates_available=False,
            dedup_key=dedup_key,
        )

    task = await enqueue(
        db,
        kind=KIND_VET_RELATIONS,
        payload={
            "entry_id": entry_id,
            "cap": max(1, limit),
            "min_observation": min_observation,
            "scheduled_by": "discover:explicit",
        },
        dedup_key=dedup_key,
        # User-triggered background work: below online chat/ingest, above
        # periodic maintenance.
        priority=DISCOVER_VET_PRIORITY,
    )
    return DirectVetScheduleResult(
        requested=True,
        candidates_available=True,
        task_id=task.id if task is not None else None,
        dedup_key=dedup_key,
    )
