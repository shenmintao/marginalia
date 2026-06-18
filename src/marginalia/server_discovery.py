from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

STATE_RELATIVE_PATH = Path("runtime") / "server.json"


def state_path(home: str | os.PathLike[str]) -> Path:
    return Path(home).expanduser() / STATE_RELATIVE_PATH


def client_base_url(host: str, port: int) -> str:
    client_host = host.strip() or "127.0.0.1"
    if client_host in {"0.0.0.0", "::"}:
        client_host = "127.0.0.1"
    if ":" in client_host and not client_host.startswith("["):
        client_host = f"[{client_host}]"
    return f"http://{client_host}:{int(port)}"


def write_server_state(
    home: str | os.PathLike[str],
    *,
    host: str,
    port: int,
    pid: int | None = None,
) -> dict[str, Any]:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "base_url": client_base_url(host, port),
        "host": host,
        "port": int(port),
        "pid": int(pid if pid is not None else os.getpid()),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "home": str(Path(home).expanduser()),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return payload


def read_server_state(home: str | os.PathLike[str]) -> dict[str, Any] | None:
    path = state_path(home)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    base_url = payload.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return None
    return payload


def clear_server_state(
    home: str | os.PathLike[str],
    *,
    pid: int | None = None,
) -> None:
    path = state_path(home)
    if pid is not None:
        payload = read_server_state(home)
        if payload is not None and payload.get("pid") not in (pid, str(pid)):
            return
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


async def discover_server_url(
    home: str | os.PathLike[str],
    *,
    timeout_seconds: float = 0.6,
) -> str | None:
    payload = read_server_state(home)
    if payload is None:
        return None
    base_url = str(payload["base_url"]).rstrip("/")
    try:
        async with httpx.AsyncClient(base_url=base_url, timeout=timeout_seconds) as client:
            response = await client.get("/health")
        if response.status_code == 200:
            return base_url
    except (httpx.HTTPError, OSError, ValueError):
        return None
    return None
