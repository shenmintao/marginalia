"""HTTP client for the Marginalia server.

Used by the CLI REPL and slash commands. Thin wrapper around httpx —
methods correspond 1:1 to server endpoints.

The constructor accepts an optional `transport` parameter so tests can
inject httpx.ASGITransport for in-memory end-to-end testing without a
running server.

All business endpoints sit under `/v1/`. The unversioned `/health`
endpoint is the only exception.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import httpx


@dataclass(slots=True)
class ChatEvent:
    """One SSE frame from `POST /v1/chat/{session_id}`.

    event_type values: conversation / planning / plan / thinking /
    tool_call / tool_result / answer / error / done. See AgentEvent
    docstring in marginalia.agent.types for payload semantics.
    """

    event_type: str
    data: str


class MarginaliaClient:
    """Thin HTTP wrapper. One AsyncClient is held for the CLI's lifetime."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- meta ----------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        r = await self._http.get("/health")
        r.raise_for_status()
        return r.json()

    async def running_task_count(self) -> dict[str, int]:
        """Count tasks still on the queue. Used at exit to ask the user
        whether to wait. Returns {'running': N, 'pending': N}.
        """
        r = await self._http.get("/v1/tasks/running-count")
        r.raise_for_status()
        return r.json()

    async def list_active_tasks(self, limit: int = 30) -> dict[str, list[dict]]:
        """Snapshot of running + pending tasks (kind / label / age). Used by
        the `/background` REPL command so users can see what the worker is
        actually doing rather than just a count."""
        r = await self._http.get("/v1/tasks/active", params={"limit": limit})
        r.raise_for_status()
        return r.json()

    async def tend_start(self) -> dict[str, Any]:
        """Kick off a maintenance pass. Returns {tend_run_id, tasks: [...]}."""
        r = await self._http.post("/v1/tend")
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def tend_status(self, run_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/v1/tend/{run_id}")
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    # ---- folders -------------------------------------------------------------

    async def list_folder(self, parent_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        r = await self._http.get("/v1/folders", params=params)
        r.raise_for_status()
        return r.json()

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/v1/folders/{folder_id}")
        r.raise_for_status()
        return r.json()

    # ---- upload --------------------------------------------------------------

    async def upload_file(
        self,
        *,
        local_path: str | Path,
        remote_path: str,
        display_name: str | None = None,
        on_conflict: str | None = None,
    ) -> dict[str, Any]:
        local = Path(local_path)
        if not local.is_file():
            raise ValueError(f"not a file: {local}")
        params: dict[str, Any] = {"remote_path": remote_path}
        if on_conflict is not None:
            params["on_conflict"] = on_conflict
        if display_name is not None:
            params["display_name"] = display_name
        with local.open("rb") as fh:
            files = {"file": (local.name, fh.read(), "application/octet-stream")}
        r = await self._http.post("/v1/upload", params=params, files=files)
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    # ---- sessions / chat -----------------------------------------------------

    async def create_session(
        self, *, initiating_user_message: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if initiating_user_message is not None:
            body["initiating_user_message"] = initiating_user_message
        r = await self._http.post("/v1/sessions", json=body)
        r.raise_for_status()
        return r.json()

    async def stream_chat(
        self, session_id: str, query: str
    ) -> AsyncIterator[ChatEvent]:
        """Stream agent events for one chat turn.

        SSE wire format: lines `event: <type>` and `data: <payload>`,
        blank line ends one frame. We coalesce multi-line `data:` into a
        single string with `\\n` joins (sse-starlette will only emit
        single-line data for our payloads, but we handle both).
        """
        async with self._http.stream(
            "POST",
            f"/v1/chat/{session_id}",
            json={"query": query},
            timeout=None,
        ) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise CliHttpError(
                    r.status_code, body.decode("utf-8", "replace")
                )
            event_type = "message"
            data_lines: list[str] = []
            async for line in r.aiter_lines():
                if line == "":
                    if data_lines or event_type != "message":
                        yield ChatEvent(
                            event_type=event_type, data="\n".join(data_lines)
                        )
                    event_type = "message"
                    data_lines = []
                elif line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                # other SSE fields (`id:`, `retry:`, comments) ignored

    async def close_session(self, session_id: str) -> dict[str, Any]:
        r = await self._http.post(f"/v1/sessions/{session_id}/close")
        r.raise_for_status()
        return r.json()

    # ---- user-side file ops --------------------------------------------------

    async def search(self, q: str, limit: int = 25) -> dict[str, Any]:
        r = await self._http.get(
            "/v1/search", params={"q": q, "limit": limit}
        )
        r.raise_for_status()
        return r.json()

    async def discover(
        self, entry_id: str, top_k: int = 8,
        include_unvetted: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"top_k": top_k}
        if include_unvetted:
            params["include_unvetted"] = "true"
        r = await self._http.get(
            f"/v1/discover/{entry_id}", params=params,
        )
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def get_entry_metadata(self, entry_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/v1/file-entries/{entry_id}/metadata")
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def download_entry(
        self, entry_id: str, *, dest: Path
    ) -> dict[str, Any]:
        async with self._http.stream(
            "GET", f"/v1/file-entries/{entry_id}/download"
        ) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise CliHttpError(r.status_code, body.decode("utf-8", "replace"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
            return {
                "saved_to": str(dest),
                "bytes_written": total,
                "content_type": r.headers.get("content-type"),
                "file_id": r.headers.get("x-file-id"),
            }

    async def download_folder(
        self, folder_id: str, *, dest: Path
    ) -> dict[str, Any]:
        async with self._http.stream(
            "GET", f"/v1/folders/{folder_id}/download"
        ) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise CliHttpError(r.status_code, body.decode("utf-8", "replace"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
            return {
                "saved_to": str(dest),
                "bytes_written": total,
                "content_type": r.headers.get("content-type"),
                "folder_id": r.headers.get("x-folder-id"),
                "member_count": int(r.headers.get("x-member-count") or 0),
            }

    async def latest_conversation(self) -> dict[str, Any] | None:
        r = await self._http.get("/v1/conversations/latest")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def export_conversation(
        self, conversation_id: str, *, dest: Path
    ) -> dict[str, Any]:
        async with self._http.stream(
            "GET", f"/v1/conversations/{conversation_id}/export"
        ) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise CliHttpError(r.status_code, body.decode("utf-8", "replace"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with dest.open("wb") as fh:
                async for chunk in r.aiter_bytes():
                    fh.write(chunk)
                    total += len(chunk)
            return {
                "saved_to": str(dest),
                "bytes_written": total,
                "conversation_id": r.headers.get("x-conversation-id"),
                "citation_count": int(r.headers.get("x-citation-count") or 0),
                "missing_count": int(r.headers.get("x-missing-count") or 0),
            }

    async def export_conversation_markdown(
        self, conversation_id: str, *, dest: Path
    ) -> dict[str, Any]:
        """Single-file markdown export with citations rewritten inline.

        Distinct from `export_conversation` (zip): the .md endpoint
        produces a self-contained document, no references folder. Use
        when sharing a one-off result rather than archiving sources."""
        r = await self._http.get(
            f"/v1/conversations/{conversation_id}/export.md"
        )
        if r.status_code >= 400:
            raise CliHttpError(
                r.status_code,
                r.json() if _is_json(r) else r.text,
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = r.content
        dest.write_bytes(body)
        return {
            "saved_to": str(dest),
            "bytes_written": len(body),
            "conversation_id": r.headers.get("x-conversation-id"),
            "citation_count": int(r.headers.get("x-citation-count") or 0),
            "missing_count": int(r.headers.get("x-missing-count") or 0),
        }


class CliHttpError(Exception):
    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


def _is_json(r: httpx.Response) -> bool:
    return (r.headers.get("content-type") or "").startswith("application/json")
