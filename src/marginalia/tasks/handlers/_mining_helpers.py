"""Shared helpers for mining handlers.

Three miners (session_cooccurrence, tag_overlap, citation_graph) emit
candidate edges with the same upsert shape:

  - if (entry_a_id, entry_b_id) exists: bump observation_count by N,
    update last_observed_at, and replace source_kind/note only when this
    source is at least as strong as the existing attribution
  - else: insert a fresh row with observation_count=N, audit
    kind=relation_mined action=created

Centralising this avoids three copies drifting (e.g. one miner forgetting
to preserve a stronger attribution while bumping observation_count).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.db.models import EntryRelation
from marginalia.repositories import audit_events as audit_events_repo
from marginalia.repositories import entry_relations as relations_repo
from marginalia.utils.ids import new_id

UpsertAction = Literal["incremented", "created"]

async def upsert_relation_pair(
    session: AsyncSession,
    *,
    entry_a_id: str,
    entry_b_id: str,
    observation_add: int,
    source_kind: str,
    note: str,
    now: datetime,
    audit_extra: Mapping[str, Any] | None = None,
) -> tuple[str, UpsertAction]:
    """Upsert a single (a, b) edge. Caller must ensure a < b for stability.

    Returns (relation_id, "incremented" | "created"). The miner-specific
    audit_extra dict is merged into the relation_mined event payload so
    each miner can attach its own signal data (jaccard score, journal
    window, etc).
    """
    existing = await relations_repo.find_pair(
        session, entry_a_id=entry_a_id, entry_b_id=entry_b_id,
    )

    if existing is not None:
        attribution_replaced = await relations_repo.bump_observation(
            session,
            relation=existing,
            new_count=(existing.observation_count or 0) + observation_add,
            last_observed_at=now,
            source_kind=source_kind,
            note=note,
        )
        rid = existing.id
        action: UpsertAction = "incremented"
    else:
        attribution_replaced = True
        rid = new_id()
        session.add(EntryRelation(
            id=rid,
            entry_a_id=entry_a_id,
            entry_b_id=entry_b_id,
            note=note,
            source_kind=source_kind,
            last_observed_at=now,
            observation_count=observation_add,
            created_at=now,
        ))
        action = "created"

    payload: dict[str, Any] = {
        "relation_id": rid,
        "entry_a_id": entry_a_id,
        "entry_b_id": entry_b_id,
        "source_kind": source_kind,
        "observation_added": observation_add,
        "action": action,
        "attribution_replaced": attribution_replaced,
    }
    if audit_extra:
        payload.update(audit_extra)
    await audit_events_repo.append(session, kind="relation_mined", payload=payload)
    return rid, action
