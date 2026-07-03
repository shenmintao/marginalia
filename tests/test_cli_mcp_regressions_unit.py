"""Regression tests for CLI/MCP fixes from audit-report-2026-07-02.

Covered bugs:
  #42  SSE parsers lstrip()ed every `data:` line, destroying payload
       indentation (both cli/client.py stream_chat and
       mcp_server._sse_events). Spec: strip exactly ONE leading space.
  #68  MCP server answered unknown notifications (no "id") with a
       JSON-RPC error frame carrying id:null — forbidden by JSON-RPC 2.0.
       Unknown REQUESTS (with id) must still get METHOD_NOT_FOUND.
  #14  run_stdio_server dispatched requests strictly serially, so a slow
       tools/call blocked ping/tools/list until it finished.
  #69  MarginaliaClient.upload_file did not expanduser() the local path,
       so the documented "~/file.pdf" example always failed verbatim.
  #44  /ls discarded the "entries" key of /v1/folders and printed
       "(no folders)" for folders containing only files.

Pure unit tests: no DB, no MARGINALIA_HOME, in-memory transports only.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import queue
from pathlib import Path
from typing import Any

import httpx
import pytest

from marginalia import mcp_server
from marginalia.cli.client import MarginaliaClient
from marginalia.cli.commands import CliContext, cmd_ls


# ---- helpers ---------------------------------------------------------------


class _FakeSseResponse:
    """Duck-typed httpx.Response: only aiter_lines() is used by _sse_events."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _ScriptedStdin:
    """stdin stub for run_stdio_server.

    readline() is called via loop.run_in_executor, so a blocking
    queue.Queue.get() is exactly right: it parks the executor thread
    until the test feeds the next line (or "" for EOF).
    """

    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue()

    def feed(self, message: dict[str, Any]) -> None:
        self._q.put(json.dumps(message) + "\n")

    def close(self) -> None:
        self._q.put("")  # EOF

    def readline(self) -> str:
        return self._q.get()


class _FrameCollector:
    """stdout stub: parses each JSON-RPC frame the server writes."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    def write(self, s: str) -> None:
        s = s.strip()
        if s:
            self.frames.append(json.loads(s))

    def flush(self) -> None:
        pass

    def by_id(self, request_id: Any) -> list[dict[str, Any]]:
        return [f for f in self.frames if f.get("id") == request_id]


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout=timeout)


async def _finish_server(server: asyncio.Task, stdin: _ScriptedStdin) -> None:
    """Always unblock the executor thread and reap the server task."""
    stdin.close()
    try:
        await asyncio.wait_for(server, timeout=5.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server


# ---- bug #42: SSE one-space stripping ---------------------------------------

SSE_LINES = [
    "event: answer",
    "data: top-level line",
    "data:   indented sub-item",  # three chars after ':' -> keep TWO spaces
    "data:no-leading-space",
    "",
]
EXPECTED_DATA = "top-level line\n  indented sub-item\nno-leading-space"


@pytest.mark.asyncio
async def test_cli_stream_chat_strips_exactly_one_sse_space() -> None:
    body = ("\n".join(SSE_LINES) + "\n").encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body,
        )

    client = MarginaliaClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    try:
        events = [
            ev async for ev in client.stream_chat("sess-1", "indent probe")
        ]
    finally:
        await client.aclose()

    assert [ev.event_type for ev in events] == ["answer"]
    # Old behavior lstrip()ed each data line -> "indented sub-item" lost
    # its two leading spaces.
    assert events[0].data == EXPECTED_DATA


@pytest.mark.asyncio
async def test_mcp_sse_events_strips_exactly_one_sse_space() -> None:
    events = [
        (event_type, data)
        async for event_type, data in mcp_server._sse_events(
            _FakeSseResponse(SSE_LINES)  # type: ignore[arg-type]
        )
    ]
    assert events == [("answer", EXPECTED_DATA)]


# ---- bug #68: notifications are silently ignored ----------------------------


@pytest.mark.asyncio
async def test_handle_message_ignores_unknown_notification() -> None:
    # Old behavior: raised JsonRpcError(METHOD_NOT_FOUND), which the stdio
    # loop turned into an id:null error frame.
    response = await mcp_server.handle_message(
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": "t1", "progress": 1},
        }
    )
    assert response is None

    # Unknown REQUESTS (with id) must still be rejected loudly.
    with pytest.raises(mcp_server.JsonRpcError) as exc_info:
        await mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 7, "method": "totally/unknown"}
        )
    assert exc_info.value.code == mcp_server.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_stdio_server_emits_no_frame_for_unknown_notification() -> None:
    stdin = _ScriptedStdin()
    out = _FrameCollector()
    server = asyncio.create_task(
        mcp_server.run_stdio_server(stdin=stdin, stdout=out)  # type: ignore[arg-type]
    )
    try:
        stdin.feed(
            {
                "jsonrpc": "2.0",
                "method": "notifications/roots/list_changed",
            }
        )
        stdin.feed({"jsonrpc": "2.0", "id": "u1", "method": "totally/unknown"})
    finally:
        await _finish_server(server, stdin)

    # Exactly one frame: METHOD_NOT_FOUND for the unknown REQUEST.
    assert len(out.frames) == 1, out.frames
    error_frame = out.by_id("u1")[0]
    assert error_frame["error"]["code"] == mcp_server.METHOD_NOT_FOUND
    # The notification produced NO frame — in particular no id:null error.
    assert out.by_id(None) == []


# ---- bug #14: concurrent dispatch keeps ping responsive ----------------------


@pytest.mark.asyncio
async def test_ping_answered_while_slow_tools_call_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_call(
        name: str, arguments: Any, **kwargs: Any
    ) -> dict[str, Any]:
        entered.set()
        await release.wait()
        return {
            "content": [{"type": "text", "text": json.dumps({"ok": True})}],
            "isError": False,
        }

    monkeypatch.setattr(mcp_server, "call_mcp_tool", slow_call)

    stdin = _ScriptedStdin()
    out = _FrameCollector()
    server = asyncio.create_task(
        mcp_server.run_stdio_server(stdin=stdin, stdout=out)  # type: ignore[arg-type]
    )
    try:
        stdin.feed(
            {
                "jsonrpc": "2.0",
                "id": "slow",
                "method": "tools/call",
                "params": {"name": "search_journal", "arguments": {}},
            }
        )
        await asyncio.wait_for(entered.wait(), timeout=5.0)

        # The tool call is parked on `release`. Old serial loop never even
        # read the next line here, so this wait timed out.
        stdin.feed({"jsonrpc": "2.0", "id": "ping-1", "method": "ping"})
        await _wait_until(lambda: out.by_id("ping-1"))

        ping_frame = out.by_id("ping-1")[0]
        assert ping_frame == {"jsonrpc": "2.0", "id": "ping-1", "result": {}}
        # The slow call must still be in flight when the ping is answered.
        assert out.by_id("slow") == []

        release.set()
        await _wait_until(lambda: out.by_id("slow"))
        slow_frame = out.by_id("slow")[0]
        assert slow_frame["result"]["isError"] is False
    finally:
        release.set()
        await _finish_server(server, stdin)


# ---- bug #69: upload_file expands ~ ------------------------------------------


@pytest.mark.asyncio
async def test_upload_file_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:  # never reached
        return httpx.Response(200, json={})

    client = MarginaliaClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError) as exc_info:
            await client.upload_file(
                local_path="~/missing-upload-probe.pdf",
                remote_path="/papers/missing-upload-probe.pdf",
            )
    finally:
        await client.aclose()

    message = str(exc_info.value)
    # Old behavior: 'not a file: ~/missing-upload-probe.pdf' (literal tilde).
    assert "~" not in message
    assert str(tmp_path / "missing-upload-probe.pdf") in message


# ---- bug #44: /ls renders entries --------------------------------------------


class _FakeLsClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def list_folder(self, parent_id: str | None = None) -> dict[str, Any]:
        return self.payload


@pytest.mark.asyncio
async def test_cmd_ls_renders_entries_for_files_only_folder(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry_id = "e" * 36
    ctx = CliContext(
        client=_FakeLsClient(  # type: ignore[arg-type]
            {
                "folders": [],
                "entries": [
                    {
                        "id": entry_id,
                        "display_name": "notes.pdf",
                        "ingest_status": "done",
                    }
                ],
            }
        )
    )
    await cmd_ls(ctx, "")
    output = capsys.readouterr().out

    # Old behavior: early-returned '(no folders)' and never showed files.
    assert "no folders" not in output
    assert "(empty)" not in output
    assert "notes.pdf" in output
    assert entry_id in output
    assert "done" in output
    # /ls feeds the tab-completion cache too.
    assert ctx.seen_entry_ids == {entry_id: "notes.pdf"}


@pytest.mark.asyncio
async def test_cmd_ls_prints_empty_only_when_both_lists_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = CliContext(
        client=_FakeLsClient({"folders": [], "entries": []})  # type: ignore[arg-type]
    )
    await cmd_ls(ctx, "")
    assert "(empty)" in capsys.readouterr().out
