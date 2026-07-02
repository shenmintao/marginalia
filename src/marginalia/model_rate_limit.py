from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

_WINDOW_SECONDS = 1.0


@dataclass
class _RateLimiter:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timestamps: Deque[float] = field(default_factory=deque)


_LIMITERS: dict[str, _RateLimiter] = {}


def model_limit_key(
    *,
    kind: str,
    provider: str,
    base_url: str | None,
    model: str,
) -> str:
    base = str(base_url or "").strip().rstrip("/").lower()
    clean_provider = str(provider or "").strip().lower()
    clean_kind = str(kind or "").strip().lower()
    clean_model = str(model or "").strip()
    return f"{clean_kind}|{clean_provider}|{base}|{clean_model}"


async def acquire_model_call_slot(
    *,
    kind: str,
    provider: str,
    base_url: str | None,
    model: str,
    tps: int,
) -> float:
    key = model_limit_key(
        kind=kind,
        provider=provider,
        base_url=base_url,
        model=model,
    )
    limit = max(1, int(tps or 1))
    limiter = _LIMITERS.setdefault(key, _RateLimiter())
    loop = asyncio.get_running_loop()
    waited = 0.0

    while True:
        async with limiter.lock:
            now = loop.time()
            while limiter.timestamps and now - limiter.timestamps[0] >= _WINDOW_SECONDS:
                limiter.timestamps.popleft()
            if len(limiter.timestamps) < limit:
                limiter.timestamps.append(now)
                return waited
            sleep_for = max(_WINDOW_SECONDS - (now - limiter.timestamps[0]), 0.0)
        if sleep_for > 0:
            waited += sleep_for
            await asyncio.sleep(sleep_for)


def reset_model_rate_limiters_for_tests() -> None:
    _LIMITERS.clear()
