from __future__ import annotations

import os
import sys

from marginalia import server_main


def test_server_main_uses_sys_argv_when_argv_is_omitted() -> None:
    captured: dict[str, object] = {}
    runtime_env: dict[str, str | None] = {}

    def _fake_run(app: str, **kwargs) -> None:
        captured["app"] = app
        captured.update(kwargs)

    real_argv = sys.argv[:]
    real_run = server_main.uvicorn.run
    old_env = {
        key: os.environ.get(key)
        for key in ("MARGINALIA_API_HOST", "MARGINALIA_API_PORT", "MARGINALIA_HTTP_SERVER")
    }
    try:
        server_main.uvicorn.run = _fake_run  # type: ignore[assignment]
        sys.argv = [
            "python -m marginalia",
            "--host",
            "0.0.0.0",
            "--port",
            "8765",
            "--log-level",
            "warning",
        ]

        rc = server_main.main(prog="python -m marginalia")
        for key in ("MARGINALIA_API_HOST", "MARGINALIA_API_PORT", "MARGINALIA_HTTP_SERVER"):
            runtime_env[key] = os.environ.get(key)
    finally:
        server_main.uvicorn.run = real_run  # type: ignore[assignment]
        sys.argv = real_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert rc == 0
    assert captured["app"] == "marginalia.main:app"
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8765
    assert captured["log_level"] == "warning"
    assert runtime_env["MARGINALIA_API_HOST"] == "0.0.0.0"
    assert runtime_env["MARGINALIA_API_PORT"] == "8765"
    assert runtime_env["MARGINALIA_HTTP_SERVER"] == "1"
