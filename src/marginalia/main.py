from __future__ import annotations

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
from marginalia.api.routes_upload import router as upload_router
from marginalia.api.routes_user_files import router as user_files_router
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
