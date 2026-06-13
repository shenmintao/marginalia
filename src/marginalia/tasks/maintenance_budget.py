"""Background maintenance token budget helpers."""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from marginalia.config import Settings
from marginalia.db.models import TaskOutcome
from marginalia.repositories.task_outcomes import (
    GLOBAL_OBJECT_ID,
    GLOBAL_OBJECT_KIND,
    record_outcome,
)
from marginalia.tasks.kinds import (
    KIND_PROPOSE_VIEWS,
    KIND_REFRESH_ENTRY_EXTRA,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_SUMMARIZE_SESSION,
    KIND_SUGGEST_LIFECYCLE,
    KIND_TAG_QUALITY,
    KIND_VET_RELATIONS,
)


MAINTENANCE_BUDGET_WINDOW = timedelta(days=1)
MAINTENANCE_BUDGET_SKIP_REASON = "maintenance_budget_exhausted"

# These kinds are background LLM maintenance and count against the rolling
# maintenance budget when their runner usage row is recorded.
BUDGETED_MAINTENANCE_KINDS: frozenset[str] = frozenset({
    KIND_SUMMARIZE_SESSION,
    KIND_TAG_QUALITY,
    KIND_RESTRUCTURE_CATALOGS,
    KIND_SUGGEST_LIFECYCLE,
    KIND_VET_RELATIONS,
    KIND_PROPOSE_VIEWS,
    KIND_REFRESH_ENTRY_EXTRA,
})

# The budget only suppresses low-priority speculative maintenance. User-facing
# ingest/reflect tasks are intentionally outside both sets.
LOW_PRIORITY_MAINTENANCE_KINDS: frozenset[str] = frozenset({
    KIND_RESTRUCTURE_CATALOGS,
    KIND_VET_RELATIONS,
    KIND_PROPOSE_VIEWS,
})


@dataclass(slots=True, frozen=True)
class MaintenanceBudgetState:
    enabled: bool
    limit: int
    used: int
    remaining: int | None
    window_start: datetime

    @property
    def exhausted(self) -> bool:
        return self.enabled and self.used >= self.limit

    def to_detail(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "window_start": self.window_start.isoformat(),
            "window_seconds": int(MAINTENANCE_BUDGET_WINDOW.total_seconds()),
            "budgeted_kinds": sorted(BUDGETED_MAINTENANCE_KINDS),
            "low_priority_kinds": sorted(LOW_PRIORITY_MAINTENANCE_KINDS),
        }


def maintenance_budget_limit(settings: Settings) -> int:
    return max(0, int(settings.maintenance_daily_token_budget or 0))


async def read_maintenance_budget(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime,
) -> MaintenanceBudgetState:
    limit = maintenance_budget_limit(settings)
    window_start = _aware(now) - MAINTENANCE_BUDGET_WINDOW
    if limit <= 0:
        return MaintenanceBudgetState(
            enabled=False,
            limit=0,
            used=0,
            remaining=None,
            window_start=window_start,
        )
    used = await maintenance_tokens_used_since(session, since=window_start)
    return MaintenanceBudgetState(
        enabled=True,
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        window_start=window_start,
    )


async def maintenance_tokens_used_since(
    session: AsyncSession,
    *,
    since: datetime,
) -> int:
    rows = (
        await session.execute(
            select(TaskOutcome.detail).where(
                TaskOutcome.object_kind == "task",
                TaskOutcome.task_kind.in_(sorted(BUDGETED_MAINTENANCE_KINDS)),
                TaskOutcome.completed_at >= since,
            )
        )
    ).scalars().all()
    total = 0
    for raw in rows:
        detail = _detail_mapping(raw)
        total += _as_int(detail.get("tokens_in"))
        total += _as_int(detail.get("tokens_out"))
    return total


async def should_defer_for_budget(
    session: AsyncSession,
    *,
    kind: str,
    state: MaintenanceBudgetState,
) -> bool:
    if kind not in LOW_PRIORITY_MAINTENANCE_KINDS or not state.exhausted:
        return False
    if await _already_recorded_budget_defer(
        session,
        kind=kind,
        since=state.window_start,
    ):
        return True
    await record_outcome(
        session,
        task_kind=kind,
        object_kind=GLOBAL_OBJECT_KIND,
        object_id=GLOBAL_OBJECT_ID,
        outcome="deferred",
        detail={
            "reason": MAINTENANCE_BUDGET_SKIP_REASON,
            "message": "MAINTENANCE_DAILY_TOKEN_BUDGET exhausted",
            "maintenance_daily_token_budget": state.limit,
            "maintenance_tokens_used": state.used,
            "maintenance_tokens_remaining": state.remaining,
            "window_start": state.window_start.isoformat(),
            "scheduled_by": "periodic_tick",
        },
    )
    return True


async def _already_recorded_budget_defer(
    session: AsyncSession,
    *,
    kind: str,
    since: datetime,
) -> bool:
    rows = (
        await session.execute(
            select(TaskOutcome.detail).where(
                TaskOutcome.task_kind == kind,
                TaskOutcome.object_kind == GLOBAL_OBJECT_KIND,
                TaskOutcome.object_id == GLOBAL_OBJECT_ID,
                TaskOutcome.outcome == "deferred",
                TaskOutcome.completed_at >= since,
            )
        )
    ).scalars().all()
    return any(
        _detail_mapping(raw).get("reason") == MAINTENANCE_BUDGET_SKIP_REASON
        for raw in rows
    )


def _detail_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
