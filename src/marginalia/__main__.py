"""Entry point for the packaged backend sidecar."""

from __future__ import annotations

from marginalia.server_main import main


if __name__ == "__main__":
    raise SystemExit(main(prog="python -m marginalia"))
