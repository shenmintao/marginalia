from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


LlmProvider = Literal["openai", "anthropic"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"

    db_backend: Literal["sqlite", "postgres"] = "sqlite"
    sqlite_path: str = "./data/marginalia.db"
    postgres_dsn: str = "postgresql+asyncpg://marginalia:marginalia@localhost:5432/marginalia"

    storage_backend: Literal["local", "s3"] = "local"
    local_storage_root: str = "./data/objects"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "marginalia"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 4
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 20

    # --- LLM defaults (used when a profile leaves a field blank) ------------
    llm_default_provider: LlmProvider = "openai"
    llm_default_api_key: str | None = None
    llm_default_base_url: str | None = None
    llm_default_model: str = "gpt-4o-mini"

    # --- Per-profile overrides (chat / reflect / ingest / vision / audio) ---
    # Any field left blank inherits the corresponding `llm_default_*` value.
    # `audio` is text-transcription only (Whisper et al.) — provider must be
    # OpenAI-compatible since Anthropic has no transcription API.
    llm_chat_provider: LlmProvider | None = None
    llm_chat_api_key: str | None = None
    llm_chat_base_url: str | None = None
    llm_chat_model: str | None = None

    llm_reflect_provider: LlmProvider | None = None
    llm_reflect_api_key: str | None = None
    llm_reflect_base_url: str | None = None
    llm_reflect_model: str | None = None

    llm_ingest_provider: LlmProvider | None = None
    llm_ingest_api_key: str | None = None
    llm_ingest_base_url: str | None = None
    llm_ingest_model: str | None = None

    llm_vision_provider: LlmProvider | None = None
    llm_vision_api_key: str | None = None
    llm_vision_base_url: str | None = None
    llm_vision_model: str | None = None

    llm_audio_provider: LlmProvider | None = None  # only "openai" makes sense
    llm_audio_api_key: str | None = None
    llm_audio_base_url: str | None = None
    llm_audio_model: str | None = None

    @property
    def database_url(self) -> str:
        if self.db_backend == "sqlite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        return self.postgres_dsn


@dataclass(slots=True, frozen=True)
class LlmProfile:
    name: str
    provider: LlmProvider
    api_key: str | None
    base_url: str | None
    model: str


def resolve_profile(settings: Settings, profile: str) -> LlmProfile:
    """Resolve `profile` ('chat'/'reflect'/'ingest'/'vision'/'audio') against
    `LLM_<PROFILE>_*` overrides, falling back to `LLM_DEFAULT_*` per-field."""
    if profile not in ("chat", "reflect", "ingest", "vision", "audio"):
        raise ValueError(f"unknown LLM profile: {profile!r}")

    p = profile
    provider = getattr(settings, f"llm_{p}_provider") or settings.llm_default_provider
    api_key = getattr(settings, f"llm_{p}_api_key") or settings.llm_default_api_key
    base_url = getattr(settings, f"llm_{p}_base_url") or settings.llm_default_base_url
    model = getattr(settings, f"llm_{p}_model") or settings.llm_default_model

    return LlmProfile(
        name=p, provider=provider, api_key=api_key, base_url=base_url, model=model
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
