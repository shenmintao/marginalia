"""Stdio MCP server exposing Marginalia workflow tools."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote
from uuid import uuid4

import httpx

from marginalia.agent.tools import ToolContext, ToolRegistration, all_tool_defs, get_tool
from marginalia.db.session import session_scope

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "marginalia"

READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    "recall_knowledge",
    "read_files",
    "search_metadata",
    "search_journal",
    "read_entries_metadata",
    "list_folder",
    "list_catalogs",
    "read_catalog",
    "resolve_tag",
    "materialize_view",
)
READ_ONLY_TOOL_SET = set(READ_ONLY_TOOL_NAMES)

WORKFLOW_TOOL_NAMES: tuple[str, ...] = (
    "ask_marginalia",
    "search_files",
    "get_file_metadata",
    "upload_file",
    "download_file",
    "download_folder",
    "export_conversation",
)
WORKFLOW_TOOL_SET = set(WORKFLOW_TOOL_NAMES)

JSONRPC_VERSION = "2.0"
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
ENV_SERVER = "MARGINALIA_SERVER"
ENV_API_TOKEN = "MARGINALIA_API_TOKEN"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _server_version() -> str:
    try:
        from marginalia import __version__
    except Exception:  # noqa: BLE001
        return "unknown"
    return __version__


def _jsonrpc_result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _jsonrpc_error(
    request_id: Any,
    code: int,
    message: str,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def _text_content(payload: Any) -> list[dict[str, str]]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return [{"type": "text", "text": text}]


class McpBackendError(RuntimeError):
    pass


def _mcp_tool(reg: ToolRegistration) -> dict[str, Any]:
    return {
        "name": reg.name,
        "description": reg.description,
        "inputSchema": reg.input_schema,
    }


def _workflow_tool_defs() -> list[dict[str, Any]]:
    return [
        {
            "name": "ask_marginalia",
            "description": (
                "Ask Marginalia a citation-grounded research question through "
                "the shared backend. Returns the final answer and conversation id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "mode": {"type": "string", "enum": ["auto", "quick", "deep"]},
                    "session_id": {"type": "string"},
                    "close_session": {"type": "boolean"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "search_files",
            "description": "Search user-visible file entries by text.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
        {
            "name": "get_file_metadata",
            "description": "Read user-visible metadata for a file entry.",
            "inputSchema": {
                "type": "object",
                "properties": {"entry_id": {"type": "string"}},
                "required": ["entry_id"],
                "additionalProperties": False,
            },
        },
        {
            "name": "upload_file",
            "description": (
                "Upload a local file into the Marginalia library. Provide either "
                "remote_path or folder_id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "local_path": {"type": "string"},
                    "remote_path": {"type": "string"},
                    "folder_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "on_conflict": {
                        "type": "string",
                        "enum": ["rename", "error", "skip"],
                    },
                },
                "required": ["local_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "download_file",
            "description": "Download a file entry to a local destination path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                    "destination_path": {"type": "string"},
                },
                "required": ["entry_id", "destination_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "download_folder",
            "description": "Download a folder as a zip archive to a local path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "folder_id": {"type": "string"},
                    "destination_path": {"type": "string"},
                },
                "required": ["folder_id", "destination_path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "export_conversation",
            "description": (
                "Export a finished conversation as markdown or a zip archive. "
                "If conversation_id is omitted, exports the latest finished turn."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "conversation_id": {"type": "string"},
                    "destination_path": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "zip"]},
                },
                "required": ["destination_path"],
                "additionalProperties": False,
            },
        },
    ]


def list_agent_mcp_tools() -> list[dict[str, Any]]:
    by_name = {
        tool_def.name: tool_def
        for tool_def in all_tool_defs()
        if tool_def.name in READ_ONLY_TOOL_SET
    }
    return [
        {
            "name": name,
            "description": by_name[name].description,
            "inputSchema": by_name[name].input_schema,
        }
        for name in READ_ONLY_TOOL_NAMES
        if name in by_name
    ]


def list_mcp_tools() -> list[dict[str, Any]]:
    tools = list_agent_mcp_tools()
    tools.extend(_workflow_tool_defs())
    return tools


def _normalize_base_url(url: str | None) -> str | None:
    if url is None:
        return None
    normalized = url.strip().rstrip("/")
    return normalized or None


async def _discover_backend_url(
    *,
    explicit_server_url: str | None,
    discover_backend: bool,
) -> tuple[str | None, bool]:
    explicit = _normalize_base_url(explicit_server_url)
    if explicit is not None:
        return explicit, True
    if not discover_backend:
        return None, False
    env_server = _normalize_base_url(os.environ.get(ENV_SERVER))
    if env_server is not None:
        return env_server, True
    try:
        from marginalia.config import get_settings
        from marginalia.server_discovery import discover_server_url

        settings = get_settings()
        discovered = await discover_server_url(settings.marginalia_home)
    except (OSError, RuntimeError, ValueError):
        return None, False
    return _normalize_base_url(discovered), False


class McpBackend:
    def __init__(
        self,
        *,
        server_url: str | None = None,
        api_token: str | None = None,
        discover_backend: bool = True,
    ) -> None:
        self.server_url = server_url
        self.api_token = api_token
        self.discover_backend = discover_backend
        self.mode = "unresolved"
        self._client: httpx.AsyncClient | None = None
        self._lifespan: Any | None = None
        self._client_lock = asyncio.Lock()

    async def __aenter__(self) -> "McpBackend":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._lifespan is not None:
            await self._lifespan.__aexit__(exc_type, exc, tb)
            self._lifespan = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client

        # Requests are dispatched concurrently; without the lock two first
        # callers would build (and leak) duplicate clients/lifespans.
        async with self._client_lock:
            if self._client is not None:
                return self._client

            base_url, _explicit = await _discover_backend_url(
                explicit_server_url=self.server_url,
                discover_backend=self.discover_backend,
            )
            headers = {"Authorization": f"Bearer {self.api_token}"} if self.api_token else None
            if base_url is not None:
                self.mode = "http"
                self._client = httpx.AsyncClient(
                    base_url=base_url,
                    timeout=httpx.Timeout(60.0, connect=1.0),
                    headers=headers,
                )
                return self._client

            from asgi_lifespan import LifespanManager

            from marginalia.main import app

            self.mode = "embedded"
            self._lifespan = LifespanManager(app)
            manager = await self._lifespan.__aenter__()
            self._client = httpx.AsyncClient(
                base_url="http://embedded",
                transport=httpx.ASGITransport(app=manager.app),
                timeout=60.0,
                headers=headers,
            )
            return self._client


def _mcp_json_content(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": _text_content(payload), "isError": is_error}


def _require_str(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise JsonRpcError(INVALID_PARAMS, f"{key} is required")
    return value


def _optional_str(args: Mapping[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise JsonRpcError(INVALID_PARAMS, f"{key} must be a non-empty string")
    return value


def _optional_int(args: Mapping[str, Any], key: str, default: int) -> int:
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, int):
        raise JsonRpcError(INVALID_PARAMS, f"{key} must be an integer")
    return value


def _destination_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.exists() and path.is_dir():
        raise JsonRpcError(INVALID_PARAMS, "destination_path must include a file name")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def _json_response(response: httpx.Response) -> Any:
    if response.status_code >= 400:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        raise McpBackendError(f"HTTP {response.status_code}: {payload}")
    try:
        return response.json()
    except ValueError as exc:
        raise McpBackendError("backend returned non-JSON response") from exc


async def _write_stream_to_path(
    response: httpx.Response,
    *,
    destination: Path,
) -> dict[str, Any]:
    if response.status_code >= 400:
        body = await response.aread()
        raise McpBackendError(f"HTTP {response.status_code}: {body.decode('utf-8', 'replace')}")
    total = 0
    with destination.open("wb") as fh:
        async for chunk in response.aiter_bytes():
            fh.write(chunk)
            total += len(chunk)
    return {
        "saved_to": str(destination),
        "bytes_written": total,
        "content_type": response.headers.get("content-type"),
    }


async def _call_mcp_tool_http(
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    base_url: str,
    api_token: str | None,
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
    timeout = httpx.Timeout(60.0, connect=1.0)
    try:
        async with httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
        ) as client:
            return await _call_agent_tool_http_client(client, name, arguments)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise McpBackendError(f"cannot reach MCP backend at {base_url}: {exc}") from exc


async def _call_agent_tool_http_client(
    client: httpx.AsyncClient,
    name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    path_name = quote(name, safe="")
    response = await client.post(
        f"/v1/mcp/tools/{path_name}/call",
        json={"arguments": dict(arguments or {})},
    )
    if response.status_code >= 400:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = response.text
        raise McpBackendError(f"MCP backend HTTP {response.status_code}: {payload}")
    payload = response.json()
    if not isinstance(payload, dict) or "content" not in payload:
        raise McpBackendError("MCP backend returned an invalid tool result")
    return payload


async def _sse_events(response: httpx.Response):
    event_type = "message"
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines or event_type != "message":
                yield event_type, "\n".join(data_lines)
            event_type = "message"
            data_lines = []
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            # SSE spec: strip exactly one leading space; keep further indentation.
            chunk = line[5:]
            data_lines.append(chunk[1:] if chunk.startswith(" ") else chunk)
    if data_lines or event_type != "message":
        yield event_type, "\n".join(data_lines)


async def _ask_marginalia(
    client: httpx.AsyncClient,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    query = _require_str(args, "query")
    mode = str(args.get("mode") or "auto")
    if mode not in {"auto", "quick", "deep"}:
        raise JsonRpcError(INVALID_PARAMS, "mode must be auto, quick, or deep")
    session_id = _optional_str(args, "session_id")
    close_session = bool(args.get("close_session", session_id is None))

    if session_id is None:
        payload = await _json_response(
            await client.post(
                "/v1/sessions",
                json={"initiating_user_message": query},
            )
        )
        session_id = str(payload["session_id"])

    conversation_id: str | None = None
    answer_parts: list[str] = []
    plan: str | None = None
    done: dict[str, Any] | None = None
    tool_events = 0

    try:
        async with client.stream(
            "POST",
            f"/v1/chat/{quote(session_id, safe='')}",
            json={"query": query, "mode": mode},
            timeout=None,
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise McpBackendError(
                    f"HTTP {response.status_code}: {body.decode('utf-8', 'replace')}"
                )
            async for event_type, data in _sse_events(response):
                if event_type == "conversation":
                    conversation_id = data
                elif event_type == "plan":
                    plan = data
                elif event_type == "answer":
                    answer_parts.append(data)
                elif event_type in {"tool_call", "tool_result"}:
                    tool_events += 1
                elif event_type == "done":
                    try:
                        done = json.loads(data)
                    except ValueError:
                        done = {"raw": data}
                elif event_type == "error":
                    raise McpBackendError(data)
    finally:
        # Close even when the stream errors or is cancelled, mirroring
        # oneshot._run_ask; otherwise the auto-created session leaks.
        if close_session:
            try:
                await client.post(f"/v1/sessions/{quote(session_id, safe='')}/close")
            except (httpx.HTTPError, OSError):
                pass

    return _mcp_json_content(
        {
            "session_id": session_id,
            "conversation_id": conversation_id,
            "answer": "".join(answer_parts),
            "plan": plan,
            "done": done,
            "tool_events": tool_events,
        }
    )


async def _call_workflow_tool_http(
    client: httpx.AsyncClient,
    name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    args = dict(arguments or {})

    if name == "ask_marginalia":
        return await _ask_marginalia(client, args)

    if name == "search_files":
        query = _require_str(args, "query")
        limit = _optional_int(args, "limit", 25)
        payload = await _json_response(
            await client.get("/v1/search", params={"q": query, "limit": limit})
        )
        return _mcp_json_content(payload)

    if name == "get_file_metadata":
        entry_id = _require_str(args, "entry_id")
        payload = await _json_response(
            await client.get(f"/v1/file-entries/{quote(entry_id, safe='')}/metadata")
        )
        return _mcp_json_content(payload)

    if name == "upload_file":
        local_path = Path(_require_str(args, "local_path")).expanduser()
        if not local_path.is_file():
            raise JsonRpcError(INVALID_PARAMS, f"not a file: {local_path}")
        remote_path = _optional_str(args, "remote_path")
        folder_id = _optional_str(args, "folder_id")
        if (remote_path is None) == (folder_id is None):
            raise JsonRpcError(INVALID_PARAMS, "provide exactly one of remote_path or folder_id")
        params: dict[str, str] = {}
        if remote_path is not None:
            params["remote_path"] = remote_path
        if folder_id is not None:
            params["folder_id"] = folder_id
        for key in ("display_name", "on_conflict"):
            value = _optional_str(args, key)
            if value is not None:
                params[key] = value
        with local_path.open("rb") as fh:
            files = {"file": (local_path.name, fh.read(), "application/octet-stream")}
        payload = await _json_response(await client.post("/v1/upload", params=params, files=files))
        return _mcp_json_content(payload)

    if name == "download_file":
        entry_id = _require_str(args, "entry_id")
        destination = _destination_path(_require_str(args, "destination_path"))
        async with client.stream(
            "GET",
            f"/v1/file-entries/{quote(entry_id, safe='')}/download",
        ) as response:
            payload = await _write_stream_to_path(response, destination=destination)
        payload["entry_id"] = entry_id
        payload["file_id"] = response.headers.get("x-file-id")
        return _mcp_json_content(payload)

    if name == "download_folder":
        folder_id = _require_str(args, "folder_id")
        destination = _destination_path(_require_str(args, "destination_path"))
        async with client.stream(
            "GET",
            f"/v1/folders/{quote(folder_id, safe='')}/download",
        ) as response:
            payload = await _write_stream_to_path(response, destination=destination)
        payload["folder_id"] = folder_id
        payload["member_count"] = int(response.headers.get("x-member-count") or 0)
        return _mcp_json_content(payload)

    if name == "export_conversation":
        destination = _destination_path(_require_str(args, "destination_path"))
        conversation_id = _optional_str(args, "conversation_id")
        export_format = str(args.get("format") or "markdown")
        if export_format not in {"markdown", "zip"}:
            raise JsonRpcError(INVALID_PARAMS, "format must be markdown or zip")
        if conversation_id is None:
            latest = await _json_response(await client.get("/v1/conversations/latest"))
            conversation_id = str(latest["conversation_id"])
        suffix = "export.md" if export_format == "markdown" else "export"
        async with client.stream(
            "GET",
            f"/v1/conversations/{quote(conversation_id, safe='')}/{suffix}",
        ) as response:
            payload = await _write_stream_to_path(response, destination=destination)
        payload["conversation_id"] = conversation_id
        payload["citation_count"] = int(response.headers.get("x-citation-count") or 0)
        payload["missing_count"] = int(response.headers.get("x-missing-count") or 0)
        return _mcp_json_content(payload)

    raise JsonRpcError(INVALID_PARAMS, f"unknown MCP workflow tool: {name}")


async def call_mcp_tool_local(
    name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if name not in READ_ONLY_TOOL_SET:
        raise JsonRpcError(INVALID_PARAMS, f"tool is not exposed over MCP: {name}")
    reg = get_tool(name)
    if reg is None:
        raise JsonRpcError(INVALID_PARAMS, f"unknown tool: {name}")
    args = dict(arguments or {})
    ctx = ToolContext(
        session_id="mcp",
        conversation_id=f"mcp-{uuid4().hex}",
        user_message=str(args.get("query") or args.get("text") or ""),
    )
    async with session_scope() as db:
        result = await reg.handler(db, ctx, args)
    return {
        "content": _text_content(result),
        "isError": bool(isinstance(result, dict) and result.get("error")),
    }


async def call_mcp_tool(
    name: str,
    arguments: Mapping[str, Any] | None,
    *,
    server_url: str | None = None,
    api_token: str | None = None,
    discover_backend: bool = False,
    backend: McpBackend | None = None,
) -> dict[str, Any]:
    if name not in READ_ONLY_TOOL_SET and name not in WORKFLOW_TOOL_SET:
        raise JsonRpcError(INVALID_PARAMS, f"tool is not exposed over MCP: {name}")

    if backend is not None:
        client = await backend.client()
        if name in WORKFLOW_TOOL_SET:
            return await _call_workflow_tool_http(client, name, arguments)
        return await _call_agent_tool_http_client(client, name, arguments)

    if name in WORKFLOW_TOOL_SET:
        base_url, _explicit = await _discover_backend_url(
            explicit_server_url=server_url,
            discover_backend=discover_backend,
        )
        if base_url is None:
            raise McpBackendError("workflow tools require a backend runtime")
        headers = {"Authorization": f"Bearer {api_token}"} if api_token else None
        async with httpx.AsyncClient(base_url=base_url, timeout=60.0, headers=headers) as client:
            return await _call_workflow_tool_http(client, name, arguments)

    base_url, explicit = await _discover_backend_url(
        explicit_server_url=server_url,
        discover_backend=discover_backend,
    )
    if base_url is not None:
        try:
            return await _call_mcp_tool_http(
                name,
                arguments,
                base_url=base_url,
                api_token=api_token,
            )
        except McpBackendError:
            if explicit:
                raise
    return await call_mcp_tool_local(name, arguments)


async def handle_message(
    message: Mapping[str, Any],
    *,
    server_url: str | None = None,
    api_token: str | None = None,
    discover_backend: bool = False,
    backend: McpBackend | None = None,
) -> dict[str, Any] | None:
    request_id = message.get("id")
    method = message.get("method")
    if method is None:
        raise JsonRpcError(INVALID_REQUEST, "missing method")
    if not isinstance(method, str):
        raise JsonRpcError(INVALID_REQUEST, "method must be a string")

    is_notification = "id" not in message
    params = message.get("params") or {}
    if params is not None and not isinstance(params, Mapping):
        raise JsonRpcError(INVALID_PARAMS, "params must be an object")

    if method == "initialize":
        if is_notification:
            return None
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": _server_version(),
                },
            },
        )
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return None if is_notification else _jsonrpc_result(request_id, {})
    if method == "tools/list":
        return None if is_notification else _jsonrpc_result(
            request_id,
            {"tools": list_mcp_tools()},
        )
    if method == "tools/call":
        if is_notification:
            return None
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires params.name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            raise JsonRpcError(INVALID_PARAMS, "tools/call params.arguments must be an object")
        try:
            result = await call_mcp_tool(
                name,
                arguments,
                server_url=server_url,
                api_token=api_token,
                discover_backend=discover_backend,
                backend=backend,
            )
        except JsonRpcError:
            raise
        except Exception as exc:  # noqa: BLE001
            result = {
                "content": _text_content({"error": f"{type(exc).__name__}: {exc}"}),
                "isError": True,
            }
        return _jsonrpc_result(request_id, result)

    if is_notification:
        # JSON-RPC 2.0 forbids responding to notifications, even unknown ones
        # (e.g. notifications/progress, notifications/roots/list_changed).
        return None
    raise JsonRpcError(METHOD_NOT_FOUND, f"unknown method: {method}")


async def _read_line(stdin: TextIO) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, stdin.readline)


def _cancelled_request_key(message: Mapping[str, Any]) -> Any | None:
    params = message.get("params")
    if not isinstance(params, Mapping):
        return None
    request_id = params.get("requestId")
    return request_id if isinstance(request_id, (str, int, float)) else None


# How long a clean stdin EOF waits for in-flight requests to finish before
# falling back to cancellation. Generous: tools/call may run agent asks.
EOF_DRAIN_TIMEOUT_SECONDS = 300.0


async def run_stdio_server(
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    server_url: str | None = None,
    api_token: str | None = None,
) -> int:
    write_lock = asyncio.Lock()
    # in_flight maps request id -> task for notifications/cancelled lookups.
    # live_tasks tracks EVERY dispatch task so the shutdown drain in the
    # finally block cannot miss one, whatever happens to the id mapping.
    in_flight: dict[Any, asyncio.Task[None]] = {}
    live_tasks: set[asyncio.Task[None]] = set()

    async def _write(response: dict[str, Any]) -> None:
        async with write_lock:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            stdout.flush()

    def _reap(done: asyncio.Task[None], *, key: Any) -> None:
        live_tasks.discard(done)
        if in_flight.get(key) is done:
            del in_flight[key]
        if not done.cancelled():
            done.exception()  # consume, avoiding "exception was never retrieved"

    async with McpBackend(
        server_url=server_url,
        api_token=api_token,
        discover_backend=True,
    ) as backend:

        async def _dispatch(message: Mapping[str, Any], request_id: Any) -> None:
            try:
                response = await handle_message(message, backend=backend)
            except asyncio.CancelledError:
                # notifications/cancelled or shutdown: the client no longer
                # expects a response for this request.
                raise
            except JsonRpcError as exc:
                response = _jsonrpc_error(request_id, exc.code, exc.message, exc.data)
            except Exception as exc:  # noqa: BLE001
                response = _jsonrpc_error(
                    request_id,
                    INTERNAL_ERROR,
                    f"{type(exc).__name__}: {exc}",
                )
            if response is not None:
                await _write(response)

        clean_eof = False
        try:
            while True:
                line = await _read_line(stdin)
                if not line:
                    clean_eof = True
                    return 0
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    await _write(_jsonrpc_error(None, PARSE_ERROR, "invalid JSON", str(exc)))
                    continue
                if not isinstance(message, Mapping):
                    await _write(
                        _jsonrpc_error(None, INVALID_REQUEST, "message must be a JSON object")
                    )
                    continue
                if "id" not in message:
                    # JSON-RPC 2.0 forbids responding to notifications, even
                    # unknown ones. notifications/cancelled is the only one
                    # that needs work: cancel the matching in-flight request.
                    if message.get("method") == "notifications/cancelled":
                        key = _cancelled_request_key(message)
                        task = in_flight.get(key) if key is not None else None
                        if task is not None:
                            task.cancel()
                    continue
                request_id = message.get("id")
                if request_id is not None and not isinstance(request_id, (str, int, float)):
                    await _write(
                        _jsonrpc_error(None, INVALID_REQUEST, "id must be a string or number")
                    )
                    continue
                if request_id in in_flight:
                    # Dispatching would overwrite the in_flight entry, making
                    # the first task unreachable for notifications/cancelled.
                    await _write(
                        _jsonrpc_error(
                            request_id,
                            INVALID_REQUEST,
                            f"request id already in flight: {request_id!r}",
                        )
                    )
                    continue
                # Each request runs in its own task so ping/tools/list stay
                # responsive while a long tools/call is in flight.
                task = asyncio.create_task(_dispatch(message, request_id))
                in_flight[request_id] = task
                live_tasks.add(task)
                task.add_done_callback(lambda done, key=request_id: _reap(done, key=key))
        finally:
            pending = [task for task in live_tasks if not task.done()]
            try:
                if pending and clean_eof:
                    # Clean EOF (e.g. `printf '...' | marginalia mcp --stdio`):
                    # drain, so responses to requests already read are written
                    # before exit. Cancellation is reserved for the error path
                    # and for tasks that outlive the drain window.
                    _done, not_done = await asyncio.wait(
                        pending, timeout=EOF_DRAIN_TIMEOUT_SECONDS
                    )
                    pending = list(not_done)
            finally:
                for task in pending:
                    task.cancel()
                if pending:
                    # Await inside the backend context so cancelled asks can
                    # still close their auto-created sessions.
                    await asyncio.gather(*pending, return_exceptions=True)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marginalia mcp",
        description="Run a stdio MCP server exposing Marginalia workflow tools.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run over stdio. This is the only transport currently supported.",
    )
    parser.add_argument(
        "--server",
        default=None,
        help="HTTP backend URL. If omitted, uses MARGINALIA_SERVER, then "
             "discovers MARGINALIA_HOME/runtime/server.json, then falls back "
             "to an embedded backend.",
    )
    parser.add_argument(
        "--api-token",
        default=None,
        help="Bearer token for HTTP backend mode. Falls back to MARGINALIA_API_TOKEN.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    server_url = args.server or os.environ.get(ENV_SERVER) or None
    api_token = args.api_token or os.environ.get(ENV_API_TOKEN) or None
    return asyncio.run(run_stdio_server(server_url=server_url, api_token=api_token))


if __name__ == "__main__":
    raise SystemExit(main())
