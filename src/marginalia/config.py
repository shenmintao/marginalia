from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


LlmProvider = Literal["openai", "openai-compatible", "anthropic"]
# "openai"            -> OpenAI proper (supports strict json_schema)
# "openai-compatible" -> DeepSeek / Together / Groq / vllm / ollama. Same wire
#                        protocol as OpenAI, but only the basic
#                        response_format={"type":"json_object"} is supported,
#                        so the adapter injects the schema as text instead.
# "anthropic"         -> Anthropic Messages API.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"

    # Single root for all on-disk state (db, library, caches). Default
    # is ~/Marginalia. Per-component overrides below take precedence
    # when set; otherwise everything sits under marginalia_home/.
    marginalia_home: str = ""  # resolved to ~/Marginalia at runtime

    db_backend: Literal["sqlite", "postgres"] = "sqlite"
    # sqlite db file always lives at `<marginalia_home>/marginalia.db`. Not an
    # env override — relocate the whole footprint via MARGINALIA_HOME instead.
    postgres_dsn: str = "postgresql+asyncpg://marginalia:marginalia@localhost:5432/marginalia"

    # mirror = folder-tree on disk matching the user's intent; default.
    # local  = UUID-flat object pool; faster, dedup-on, less human-friendly.
    # s3     = remote object storage for multi-host deployments.
    # mirror/local always live under <marginalia_home>/{library,objects}.
    # Relocate the whole footprint via MARGINALIA_HOME, or symlink a single
    # subdir if you want db on SSD and library on a big disk.
    storage_backend: Literal["mirror", "local", "s3"] = "mirror"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "marginalia"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 20

    # Automatic active -> demoted -> archived transitions are opt-in.
    # Personal knowledge bases often prefer manual lifecycle control, while
    # team/shared deployments may want background cost management.
    auto_lifecycle_enabled: bool = False

    # Default policy when an upload / rename / move would collide with an
    # existing display_name in the same folder. `rename` suffixes ` (1)`,
    # `error` raises 409, `skip` returns the existing entry. Per-call
    # overrides on `/v1/upload` and the file-entry endpoints win when set.
    default_on_conflict: Literal["rename", "error", "skip"] = "rename"

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

    # --- Agent token budgets ------------------------------------------------
    # Per-call max_tokens for the planner / executor.
    # If the executor hits `stop_reason=max_tokens` during the final answer,
    # runtime.py can continue instead of returning a half-finished answer.
    agent_plan_max_tokens: int = 1024
    agent_execute_max_tokens: int = 2048
    agent_execute_max_turns: int = 15
    agent_final_answer_continue_turns: int = 3
    agent_final_answer_max_chars: int = 120_000

    # Bounded fan-out for ingest-time LLM work: long text/PDF chunk
    # indexing and scanned-PDF OCR page calls. Keep this conservative;
    # provider rate limits and local network bandwidth are the real cap.
    llm_ingest_concurrency: int = 10

    @property
    def database_url(self) -> str:
        if self.db_backend == "sqlite":
            from pathlib import Path
            home = Path(self.marginalia_home).expanduser()
            return f"sqlite+aiosqlite:///{home / 'marginalia.db'}"
        return self.postgres_dsn

    @property
    def mirror_vault_root(self) -> str:
        from pathlib import Path
        return str(Path(self.marginalia_home).expanduser() / "library")

    @property
    def local_storage_root(self) -> str:
        from pathlib import Path
        return str(Path(self.marginalia_home).expanduser() / "objects")


@dataclass(slots=True, frozen=True)
class LlmProfile:
    name: str
    provider: LlmProvider
    api_key: str | None
    base_url: str | None
    model: str


LLM_PROFILES: tuple[str, ...] = ("chat", "reflect", "ingest", "vision", "audio")
# Profiles users actually rely on out of the box. `vision` and `audio` are
# opt-in: vision adds figure descriptions and OCR for scanned PDFs; audio
# is only used by transcription pipelines. Pipelines that need them call
# `has_vision_profile` / equivalent and degrade gracefully when absent.

# Subset surfaced in the Settings GUI / `/v1/settings/llm` payload.
# `audio` is intentionally hidden until a transcription pipeline actually
# consumes it — exposing the form today would let users fill in keys that
# nothing reads, then file bugs about "I configured it and nothing
# happens". Keep the underlying Settings fields + `has_audio_profile`
# helper so flipping audio back on is a one-line change here.
LLM_PROFILES_VISIBLE: tuple[str, ...] = ("chat", "reflect", "ingest", "vision")
_REQUIRED_PROFILES: tuple[str, ...] = ("chat", "reflect", "ingest")


def _profile_field(settings: Settings, profile: str, field: str) -> object:
    """Read a per-profile override, falling back to the matching default."""
    override = getattr(settings, f"llm_{profile}_{field}")
    return override if override is not None else getattr(settings, f"llm_default_{field}")


def resolve_profile(settings: Settings, profile: str) -> LlmProfile:
    """Resolve `profile` (one of LLM_PROFILES) against `LLM_<PROFILE>_*`
    overrides, falling back to `LLM_DEFAULT_*` per-field."""
    if profile not in LLM_PROFILES:
        raise ValueError(f"unknown LLM profile: {profile!r}")
    return LlmProfile(
        name=profile,
        provider=_profile_field(settings, profile, "provider"),  # type: ignore[arg-type]
        api_key=_profile_field(settings, profile, "api_key"),  # type: ignore[arg-type]
        base_url=_profile_field(settings, profile, "base_url"),  # type: ignore[arg-type]
        model=_profile_field(settings, profile, "model"),  # type: ignore[arg-type]
    )


def has_vision_profile(settings: Settings | None = None) -> bool:
    """Whether the optional `vision` profile is *explicitly* configured.

    True only when the user set at least one `LLM_VISION_*` override
    (api_key / base_url / model). Inheriting the default api_key alone
    is NOT enough: the default model is often text-only (DeepSeek-V3,
    qwen-text), and silently routing vision calls to it produces 400
    errors per page from the provider rather than a useful failure.

    Pipelines that *augment* their output with VLM calls (PDF figure
    captions, scanned-PDF OCR, image indexing) check this so they can
    skip the VLM path entirely on installations that didn't configure
    one — instead of crashing or filling logs with provider errors.
    """
    return _has_optional_profile(settings, "vision")


def has_audio_profile(settings: Settings | None = None) -> bool:
    """Symmetric to `has_vision_profile` for transcription.

    The default profile is usually a chat model with no transcription
    endpoint, so falling back silently produces 404s. Audio pipelines
    check this and skip / surface a useful error when unset."""
    return _has_optional_profile(settings, "audio")


def _has_optional_profile(settings: Settings | None, profile: str) -> bool:
    s = settings if settings is not None else get_settings()
    return any(
        getattr(s, f"llm_{profile}_{field}") not in (None, "")
        for field in ("api_key", "base_url", "model")
    )


class LlmConfigError(RuntimeError):
    """Startup-time LLM configuration is incomplete or inconsistent."""


def validate_llm_config(settings: Settings) -> None:
    """Fail fast at startup if required LLM credentials are missing.

    Without this, a freshly-installed Marginalia accepts `/upload` and `/chat`
    requests but every task quietly errors when it first tries to call the
    provider — the failure shows up in `task_outcomes`, not in the foreground.

    Rule: each required profile must resolve to a non-empty api_key. A profile
    can satisfy this either via its own `LLM_<PROFILE>_API_KEY` or by inheriting
    `LLM_DEFAULT_API_KEY`.
    """
    missing = [p for p in _REQUIRED_PROFILES if not _profile_field(settings, p, "api_key")]
    if missing:
        raise LlmConfigError(
            "LLM api_key is not configured for required profile(s): "
            f"{', '.join(missing)}. Set LLM_DEFAULT_API_KEY in .env, or set "
            "the per-profile override LLM_<PROFILE>_API_KEY for each."
        )


def _default_home() -> str:
    """`~/Marginalia` cross-platform. Used when MARGINALIA_HOME is unset."""
    from pathlib import Path
    return str(Path.home() / "Marginalia")


def _resolve_paths(settings: "Settings") -> None:
    """In-place: resolve `marginalia_home` to an absolute path and ensure
    it exists.

    Without the mkdir, an unset / fresh-install MARGINALIA_HOME blows up at
    the first sqlite connect with `unable to open database file` because
    aiosqlite refuses to mkdir for you. Storage backends (mirror/local)
    handle their own subdir creation lazily, so the home dir itself is the
    only thing we guarantee here.
    """
    from pathlib import Path
    home = settings.marginalia_home or _default_home()
    home_path = Path(home).expanduser()
    settings.marginalia_home = str(home_path)
    home_path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    _resolve_paths(s)
    # Merge the GUI-writable overlay (config_overlay.json under
    # MARGINALIA_HOME) so its values take precedence over .env. Imported
    # lazily to avoid an import cycle (services -> config -> services).
    from marginalia.services.config_overlay import (
        merge_overlay_into_settings, read_overlay,
    )
    merge_overlay_into_settings(s, read_overlay(s.marginalia_home))
    return s
