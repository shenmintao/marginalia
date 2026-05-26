from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import marginalia.tasks.handlers  # noqa: F401  (registers task handlers)
from marginalia.api.routes_agent import router as sessions_router
from marginalia.api.routes_chat import router as chat_router
from marginalia.api.routes_exports import router as exports_router
from marginalia.api.routes_file_entries import router as file_entries_router
from marginalia.api.routes_files import router as files_router
from marginalia.api.routes_folders import router as folders_router
from marginalia.api.routes_settings import router as settings_router
from marginalia.api.routes_tasks import router as tasks_router
from marginalia.api.routes_tend import router as tend_router
from marginalia.api.routes_upload import router as upload_router
from marginalia.api.routes_user_files import router as user_files_router
from marginalia.config import get_settings, validate_llm_config
from marginalia.db.bootstrap import bootstrap_schema
from marginalia.db.engine import dispose_engine
from marginalia.tasks.runner import TaskRunner

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    validate_llm_config(settings)
    await bootstrap_schema()
    await _check_storage_consistency(settings)
    runner: TaskRunner | None = None
    if settings.worker_enabled:
        runner = TaskRunner(settings)
        await runner.start()
        log.info("task runner started in-process")
    try:
        yield
    finally:
        if runner is not None:
            await runner.stop()
        await dispose_engine()


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
app.include_router(settings_router, prefix=V1_PREFIX)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "storage_backend": s.storage_backend}
