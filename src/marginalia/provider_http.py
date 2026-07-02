from __future__ import annotations

import json
from typing import Any

import httpx

_MAX_PROVIDER_ERROR_CHARS = 2000


def raise_for_provider_status(response: httpx.Response, operation: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _response_error_detail(response)
        status = response.status_code
        reason = response.reason_phrase
        message = f"{operation} provider error {status} {reason}"
        if detail:
            message = f"{message}: {detail}"
        raise RuntimeError(message) from exc


def _response_error_detail(response: httpx.Response) -> str:
    try:
        body: Any = response.json()
    except Exception:
        body = response.text
    if isinstance(body, str):
        detail = body
    else:
        detail = json.dumps(body, ensure_ascii=False, sort_keys=True)
    return detail[:_MAX_PROVIDER_ERROR_CHARS]
