from __future__ import annotations

from marginalia.config import Settings, resolve_profile
from marginalia.model_rate_limit import model_limit_key
from marginalia.semantic.rerank import _rerank_endpoint


def test_resolve_profile_uses_lowest_tps_for_shared_model() -> None:
    settings = Settings(
        llm_default_provider="openai-compatible",
        llm_default_base_url="https://api.example.test/v1/",
        llm_default_model="shared-model",
        llm_default_tps=8,
        llm_chat_provider="openai-compatible",
        llm_chat_base_url="https://api.example.test/v1/",
        llm_chat_model="shared-model",
        llm_chat_tps=3,
        llm_ingest_provider="openai-compatible",
        llm_ingest_base_url="https://api.example.test/v1/",
        llm_ingest_model="shared-model",
        llm_ingest_tps=5,
    )

    profile = resolve_profile(settings, "ingest")

    assert profile.tps == 3


def test_model_limit_key_normalizes_endpoint_identity() -> None:
    left = model_limit_key(
        kind="Chat",
        provider="OpenAI-Compatible",
        base_url="https://api.example.test/v1/",
        model="qwen",
    )
    right = model_limit_key(
        kind="chat",
        provider="openai-compatible",
        base_url="https://api.example.test/v1",
        model="qwen",
    )

    assert left == right


def test_rerank_endpoint_preserves_native_bailian_url() -> None:
    native = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"

    assert _rerank_endpoint(native) == native
    assert _rerank_endpoint("https://rerank.example.test/v1") == (
        "https://rerank.example.test/v1/reranks"
    )
