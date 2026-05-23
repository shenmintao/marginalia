"""HTTP client for the Marginalia server.

Used by the CLI REPL and slash commands. Thin wrapper around httpx —
methods correspond 1:1 to server endpoints.

The constructor accepts an optional `transport` parameter so tests can
inject httpx.ASGITransport for in-memory end-to-end testing without a
running server.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import httpx


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

    # ---- folders -------------------------------------------------------------

    async def list_folders(self, parent_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        r = await self._http.get("/folders", params=params)
        r.raise_for_status()
        return r.json()

    async def get_folder(self, folder_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/folders/{folder_id}")
        r.raise_for_status()
        return r.json()

    # ---- upload --------------------------------------------------------------

    async def upload_file(
        self,
        *,
        local_path: str | Path,
        remote_path: str,
        display_name: str | None = None,
        on_conflict: str = "rename",
    ) -> dict[str, Any]:
        local = Path(local_path)
        if not local.is_file():
            raise ValueError(f"not a file: {local}")
        params: dict[str, Any] = {
            "remote_path": remote_path,
            "on_conflict": on_conflict,
        }
        if display_name is not None:
            params["display_name"] = display_name
        with local.open("rb") as fh:
            files = {"file": (local.name, fh.read(), "application/octet-stream")}
        r = await self._http.post("/upload", params=params, files=files)
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    # ---- agent ---------------------------------------------------------------

    async def create_session(
        self, *, initiating_user_message: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if initiating_user_message is not None:
            body["initiating_user_message"] = initiating_user_message
        r = await self._http.post("/sessions", json=body)
        r.raise_for_status()
        return r.json()

    async def turn(self, session_id: str, user_message: str) -> dict[str, Any]:
        r = await self._http.post(
            f"/sessions/{session_id}/turn",
            json={"user_message": user_message},
        )
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def close_session(self, session_id: str) -> dict[str, Any]:
        r = await self._http.post(f"/sessions/{session_id}/close")
        r.raise_for_status()
        return r.json()

    # ---- user-side file ops --------------------------------------------------

    async def search(self, q: str, limit: int = 25) -> dict[str, Any]:
        r = await self._http.get("/search", params={"q": q, "limit": limit})
        r.raise_for_status()
        return r.json()

    async def get_entry_metadata(self, entry_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/file-entries/{entry_id}/metadata")
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def download_entry(
        self, entry_id: str, *, dest: Path
    ) -> dict[str, Any]:
        """Download to `dest`. Returns metadata header summary."""
        async with self._http.stream(
            "GET", f"/file-entries/{entry_id}/download"
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
        """Download a folder as zip to `dest`."""
        async with self._http.stream(
            "GET", f"/folders/{folder_id}/download"
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
        """Server's most recent ended conversation, or None if none exist."""
        r = await self._http.get("/conversations/latest")
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise CliHttpError(r.status_code, r.json() if _is_json(r) else r.text)
        return r.json()

    async def export_conversation(
        self, conversation_id: str, *, dest: Path
    ) -> dict[str, Any]:
        """Export an agent conversation (report + cited files) as zip."""
        async with self._http.stream(
            "GET", f"/conversations/{conversation_id}/export"
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


class CliHttpError(Exception):
    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


def _is_json(r: httpx.Response) -> bool:
    return (r.headers.get("content-type") or "").startswith("application/json")
