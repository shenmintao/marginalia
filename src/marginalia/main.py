from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from hmac import compare_digest

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import marginalia.tasks.handlers  # noqa: F401  (registers task handlers)
from marginalia.api.routes_agent import router as sessions_router
from marginalia.api.routes_chat import router as chat_router
from marginalia.api.routes_exports import router as exports_router
from marginalia.api.routes_file_entries import router as file_entries_router
from marginalia.api.routes_files import router as files_router
from marginalia.api.routes_folders import router as folders_router
from marginalia.api.routes_mcp import router as mcp_router
from marginalia.api.routes_semantic_index import router as semantic_index_router
from marginalia.api.routes_settings import router as settings_router
from marginalia.api.routes_tasks import router as tasks_router
from marginalia.api.routes_tend import router as tend_router
from marginalia.api.routes_upload import (
    _MULTIPART_OVERHEAD as _UPLOAD_MULTIPART_OVERHEAD,
    router as upload_router,
)
from marginalia.api.routes_user_files import router as user_files_router
from marginalia.api.routes_webdav_sync import router as webdav_sync_router
from marginalia.config import LlmConfigError, get_settings, validate_llm_config
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.db.engine import dispose_engine
from marginalia.server_discovery import clear_server_state, write_server_state
from marginalia.tasks.runner import TaskRunner

log = logging.getLogger(__name__)
SLOW_REQUEST_LOG_MS = 10_000


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    state_written = False
    runner: TaskRunner | None = None
    log.info(
        "backend startup: home=%s storage_backend=%s worker_enabled=%s desktop=%s",
        settings.marginalia_home,
        settings.storage_backend,
        settings.worker_enabled,
        os.environ.get("MARGINALIA_DESKTOP") == "1",
    )
    _warn_if_unauthenticated_bind(settings)
    # Desktop launch: server must come up even when the user hasn't entered
    # an API key yet, so they can do it from the Settings page. Tasks that
    # actually need an LLM call still fail at call time. Headless / CLI
    # launches keep the historical hard-fail.
    try:
        if os.environ.get("MARGINALIA_DESKTOP") == "1":
            try:
                validate_llm_config(settings)
            except LlmConfigError as e:
                log.warning("desktop launch with incomplete LLM config: %s", e)
        else:
            validate_llm_config(settings)
        await bootstrap_schema()
        log.info("database schema ready")
        await _check_storage_consistency(settings)
        log.info("storage consistency check passed")
        if os.environ.get("MARGINALIA_HTTP_SERVER") == "1":
            host = os.environ.get("MARGINALIA_API_HOST") or settings.marginalia_api_host
            raw_port = os.environ.get("MARGINALIA_API_PORT") or str(settings.marginalia_api_port)
            try:
                port = int(raw_port)
            except ValueError:
                port = settings.marginalia_api_port
                log.warning("invalid MARGINALIA_API_PORT=%r; using %s", raw_port, port)
            state = write_server_state(
                settings.marginalia_home,
                host=host,
                port=port,
                pid=os.getpid(),
            )
            state_written = True
            log.info("server discovery state written: %s", state["base_url"])
        if settings.worker_enabled:
            # Keep the in-process runner on live settings so GUI changes to
            # WORKER_BATCH_SIZE affect new task claims without a restart.
            runner = TaskRunner()
            await runner.start()
            log.info("task runner started in-process")
    except Exception:
        log.exception("backend startup failed")
        raise
    try:
        yield
    finally:
        log.info("backend shutdown starting")
        if runner is not None:
            await runner.stop()
        if state_written:
            clear_server_state(settings.marginalia_home, pid=os.getpid())
        await dispose_engine()
        log.info("backend shutdown complete")


def _warn_if_unauthenticated_bind(settings) -> None:
    """Loud startup warning when the configured bind host is non-loopback
    and no MARGINALIA_API_TOKEN is set: every endpoint is then reachable
    unauthenticated by any host on the network. Intentionally a warning,
    not a hard failure — existing tokenless loopback deployments and
    reverse-proxy setups keep working."""
    if settings.marginalia_api_token:
        return
    host = os.environ.get("MARGINALIA_API_HOST") or settings.marginalia_api_host
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if is_loopback:
        return
    log.warning(
        "\n"
        "================================================================\n"
        "  SECURITY WARNING: MARGINALIA_API_TOKEN is not set while the\n"
        "  API is configured to bind non-loopback host %r.\n"
        "\n"
        "  EVERY endpoint is UNAUTHENTICATED: any host that can reach\n"
        "  this port can download all ingested documents, rewrite LLM\n"
        "  settings (redirecting prompts and stored API keys to an\n"
        "  attacker endpoint), upload files, and delete data.\n"
        "\n"
        "  Set MARGINALIA_API_TOKEN before exposing the API beyond\n"
        "  127.0.0.1.\n"
        "================================================================",
        host,
    )


async def _check_storage_consistency(settings) -> None:
    """Detect when STORAGE_BACKEND was switched without migrating
    existing files. UUID-shaped storage_keys imply local; relative
    paths with slashes imply mirror — mixing them silently breaks.

    Raises StorageBackendMismatchError on conflict; the error message
    points the user at `marginalia storage migrate`.
    """
    from marginalia.db.engine import get_session_factory
    from marginalia.repositories import files as files_repo

    factory = get_session_factory()
    async with factory() as s:
        sample = await files_repo.sample_live_storage_keys(s, limit=5)
    if not sample:
        return  # empty db, nothing to check

    def _looks_uuid_flat(k: str) -> bool:
        """Local backend storage_keys are 'xx/yy/<uuid>' or short test
        fixtures like '00/aa/x'. The defining property: the leading two
        segments are hex prefix dirs. Anything that starts with a real
        word segment ('research/llm/paper.pdf') is a mirror key."""
        parts = k.split("/")
        if len(parts) < 2:
            return False
        # Hex prefix dirs are short and hex-only.
        for seg in parts[:2]:
            if not (1 <= len(seg) <= 4):
                return False
            if not all(c in "0123456789abcdef" for c in seg):
                return False
        return True

    backend = settings.storage_backend
    for k in sample:
        is_uuid = _looks_uuid_flat(k)
        if backend == "mirror" and is_uuid:
            raise StorageBackendMismatchError(
                f"STORAGE_BACKEND=mirror but existing files reference "
                f"UUID storage keys (e.g. {k!r}). Either revert "
                f"STORAGE_BACKEND=local, or run:\n"
                f"  marginalia storage migrate --from local --to mirror"
            )
        if backend == "local" and not is_uuid and "/" in k:
            raise StorageBackendMismatchError(
                f"STORAGE_BACKEND=local but existing files reference "
                f"path-shaped storage keys (e.g. {k!r}). Either revert "
                f"STORAGE_BACKEND=mirror, or run:\n"
                f"  marginalia storage migrate --from mirror --to local"
            )


class StorageBackendMismatchError(RuntimeError):
    pass


app = FastAPI(title="Marginalia", lifespan=lifespan)

# Browser-based GUI (desktop/) runs on Vite dev server (5173) or via
# Tauri (tauri://localhost). Both differ in origin from the API host
# and need CORS to read responses; the CLI client (httpx ASGITransport
# or remote-mode httpx) bypasses the browser stack and is unaffected.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:1420",
        "tauri://localhost",
        # Tauri 2's webview origin varies by platform: macOS uses
        # `tauri://localhost`, Windows (WebView2) uses
        # `http://tauri.localhost`, and the wry runtime occasionally
        # serves over `https://tauri.localhost` as well. Whitelist all
        # three so the production webview can read responses regardless
        # of the host OS.
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-File-Id", "X-Size-Bytes", "X-Conversation-Id",
        "X-Citation-Count", "X-Missing-Count", "X-Folder-Id",
        "X-Member-Count",
    ],
)


class _UploadTooLargeAbort(Exception):
    """Signals the receive wrapper hit the streamed-byte budget."""


class UploadSizeLimitMiddleware:
    """Reject oversized uploads BEFORE Starlette spools the multipart body.

    The in-route cap (routes_upload) only runs after FastAPI has resolved the
    ``UploadFile`` dependency, by which point Starlette has already written the
    whole body to a SpooledTemporaryFile (rolling onto the tmp partition past
    1 MB) — so a huge or chunked upload can exhaust disk before the route sees
    it. This ASGI middleware wraps ``receive`` and stops that at the door:
    it rejects on an over-limit Content-Length up front, and counts streamed
    bytes to abort a chunked/underreported body mid-flight. Active only when
    ``upload_max_bytes`` is configured; the route keeps its own cap as the
    authoritative second line of defence.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" \
                or scope.get("path") != f"{V1_PREFIX}/upload":
            return await self.app(scope, receive, send)
        max_bytes = get_settings().upload_max_bytes
        if not max_bytes or max_bytes <= 0:
            return await self.app(scope, receive, send)
        limit = max_bytes + _UPLOAD_MULTIPART_OVERHEAD
        headers = dict(scope.get("headers") or [])
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > limit:
                    return await self._reject(send, max_bytes)
            except ValueError:
                pass

        total = 0
        response_started = False

        async def counting_receive():
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise _UploadTooLargeAbort()
            return message

        async def watching_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, watching_send)
        except _UploadTooLargeAbort:
            # The body parser hasn't produced a response yet at the point it
            # pulls the body, so we can still answer 413 ourselves.
            if not response_started:
                await self._reject(send, max_bytes)

    @staticmethod
    async def _reject(send, max_bytes: int) -> None:
        body = json.dumps({
            "detail": {
                "error": "upload_too_large",
                "max_bytes": max_bytes,
            }
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(UploadSizeLimitMiddleware)


@app.middleware("http")
async def optional_bearer_auth(request: Request, call_next):
    token = get_settings().marginalia_api_token
    if (
        not token
        or request.method == "OPTIONS"
        or request.url.path == "/health"
    ):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix) or not compare_digest(auth[len(prefix):], token):
        return JSONResponse(
            {"detail": "missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.middleware("http")
async def request_diagnostics(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        log.exception(
            "request %s failed method=%s path=%s client=%s duration_ms=%d",
            request_id,
            request.method,
            request.url.path,
            _client_host(request),
            duration_ms,
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-Id"] = request_id
    path = request.url.path
    if path != "/health" and response.status_code >= 500:
        log.error(
            "request %s returned %d method=%s path=%s client=%s duration_ms=%d",
            request_id,
            response.status_code,
            request.method,
            path,
            _client_host(request),
            duration_ms,
        )
    elif path != "/health" and duration_ms >= SLOW_REQUEST_LOG_MS:
        log.info(
            "slow request %s returned %d method=%s path=%s duration_ms=%d",
            request_id,
            response.status_code,
            request.method,
            path,
            duration_ms,
        )
    return response


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "-"

V1_PREFIX = "/v1"
app.include_router(folders_router, prefix=V1_PREFIX)
app.include_router(file_entries_router, prefix=V1_PREFIX)
app.include_router(files_router, prefix=V1_PREFIX)
app.include_router(upload_router, prefix=V1_PREFIX)
app.include_router(user_files_router, prefix=V1_PREFIX)
app.include_router(webdav_sync_router, prefix=V1_PREFIX)
app.include_router(sessions_router, prefix=V1_PREFIX)
app.include_router(chat_router, prefix=V1_PREFIX)
app.include_router(exports_router, prefix=V1_PREFIX)
app.include_router(tasks_router, prefix=V1_PREFIX)
app.include_router(tend_router, prefix=V1_PREFIX)
app.include_router(semantic_index_router, prefix=V1_PREFIX)
app.include_router(settings_router, prefix=V1_PREFIX)
app.include_router(mcp_router, prefix=V1_PREFIX)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "storage_backend": s.storage_backend}
