from __future__ import annotations

import asyncio

from marginalia.server_discovery import (
    clear_server_state,
    client_base_url,
    discover_server_url,
    read_server_state,
    state_path,
    write_server_state,
)


def test_client_base_url_normalizes_bind_all_hosts() -> None:
    assert client_base_url("0.0.0.0", 8123) == "http://127.0.0.1:8123"
    assert client_base_url("::", 8123) == "http://127.0.0.1:8123"
    assert client_base_url("::1", 8123) == "http://[::1]:8123"


def test_server_state_round_trip_and_pid_guard(tmp_path) -> None:
    payload = write_server_state(tmp_path, host="0.0.0.0", port=8123, pid=123)

    assert payload["base_url"] == "http://127.0.0.1:8123"
    assert state_path(tmp_path).is_file()
    assert read_server_state(tmp_path)["pid"] == 123

    clear_server_state(tmp_path, pid=999)
    assert state_path(tmp_path).is_file()

    clear_server_state(tmp_path, pid=123)
    assert read_server_state(tmp_path) is None


def test_discover_server_url_ignores_missing_or_invalid_state(tmp_path) -> None:
    assert asyncio.run(discover_server_url(tmp_path)) is None

    state_path(tmp_path).parent.mkdir(parents=True)
    state_path(tmp_path).write_text("{}", encoding="utf-8")

    assert asyncio.run(discover_server_url(tmp_path)) is None
