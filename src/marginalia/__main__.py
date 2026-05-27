"""Entry point for the desktop sidecar.

Tauri spawns the bundled CPython runtime with `python -m marginalia`,
so this module needs to launch the FastAPI app via uvicorn. Bind to
127.0.0.1 only — the desktop frontend is the sole client and exposing
the port to other hosts would leak local-only assumptions in the API
surface (no auth, CORS open to localhost origins).

Override host/port via MARGINALIA_API_HOST / MARGINALIA_API_PORT for
edge cases (e.g. running the sidecar on a remote dev box).
"""

from __future__ import annotations

import logging
import os

import uvicorn


def main() -> None:
    host = os.environ.get("MARGINALIA_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MARGINALIA_API_PORT", "8000"))
    log_level = os.environ.get("MARGINALIA_API_LOG_LEVEL", "info")

    logging.basicConfig(level=log_level.upper())
    uvicorn.run(
        "marginalia.main:app",
        host=host,
        port=port,
        log_level=log_level,
        access_log=False,
    )


if __name__ == "__main__":
    main()
