"""TaskRunner worker_batch_size concurrency semantics."""
from __future__ import annotations

import asyncio

import pytest

from marginalia.config import Settings
from marginalia.tasks.runner import TaskRunner


@pytest.mark.asyncio
async def test_worker_batch_size_limits_inflight_tasks() -> None:
    settings = Settings(
        worker_batch_size=2,
        worker_poll_interval_seconds=0.005,
        llm_default_api_key="sk-fake",
    )
    runner = TaskRunner(settings=settings)
    pending = [f"task-{i}" for i in range(5)]
    claim_limits: list[int] = []
    active = 0
    max_active = 0
    completed = 0

    async def fake_claim_batch(limit: int) -> list[str]:
        claim_limits.append(limit)
        claimed = pending[:limit]
        del pending[:limit]
        return claimed

    async def fake_process(task_id: str) -> None:
        nonlocal active, max_active, completed
        assert task_id.startswith("task-")
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        completed += 1
        if completed == 5:
            runner._stop.set()  # type: ignore[attr-defined]

    runner._claim_batch = fake_claim_batch  # type: ignore[method-assign]
    runner._process = fake_process  # type: ignore[method-assign]

    await asyncio.wait_for(runner._run(), timeout=2)  # type: ignore[attr-defined]

    assert completed == 5
    assert max_active <= 2
    assert claim_limits
    assert all(1 <= limit <= 2 for limit in claim_limits)
