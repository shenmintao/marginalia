from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import httpx
from openai import AsyncOpenAI

from marginalia.config import Settings, get_settings


TextType = Literal["query", "document"]


@dataclass(slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    total_tokens: int = 0


class EmbeddingConfigError(RuntimeError):
    pass


def _resolve_embedding_api_key(settings: Settings) -> str | None:
    return settings.embedding_api_key


class DashScopeEmbeddingClient:
    """Native DashScope text embedding client for Bailian text-embedding-v4."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = _resolve_embedding_api_key(self.settings)
        if not self.api_key:
            raise EmbeddingConfigError(
                "embedding api key is not configured; set EMBEDDING_API_KEY"
            )
        self.base_url = (
            self.settings.embedding_base_url
            if "/compatible-mode/" not in self.settings.embedding_base_url
            else "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"
        )
        self.model = self.settings.embedding_model
        self.dimensions = max(1, int(self.settings.embedding_dimensions or 1024))

    async def embed(
        self,
        texts: list[str],
        *,
        text_type: TextType,
    ) -> EmbeddingResult:
        clean = [str(text or "").strip() for text in texts]
        if not clean:
            return EmbeddingResult(vectors=[])
        payload = {
            "model": self.model,
            "input": {
                "texts": clean,
            },
            "parameters": {
                "dimension": self.dimensions,
                "output_type": "dense",
                "text_type": text_type,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.base_url, headers=headers, json=payload)
            resp.raise_for_status()
        obj = resp.json()
        output = obj.get("output") if isinstance(obj, dict) else None
        embeddings = output.get("embeddings") if isinstance(output, dict) else None
        if not isinstance(embeddings, list):
            raise RuntimeError("embedding response missing output.embeddings")
        ordered: list[list[float] | None] = [None] * len(clean)
        for idx, item in enumerate(embeddings):
            if not isinstance(item, dict):
                continue
            text_index = int(item.get("text_index", idx))
            vector = item.get("embedding")
            if isinstance(vector, list) and 0 <= text_index < len(ordered):
                ordered[text_index] = [float(v) for v in vector]
        vectors = [_normalize(vec or []) for vec in ordered]
        usage = obj.get("usage") if isinstance(obj, dict) else None
        total_tokens = int((usage or {}).get("total_tokens") or 0) if isinstance(usage, dict) else 0
        return EmbeddingResult(vectors=vectors, total_tokens=total_tokens)


class OpenAICompatibleEmbeddingClient:
    """OpenAI-compatible embeddings client, used by Bailian compatible-mode."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = _resolve_embedding_api_key(self.settings)
        if not self.api_key:
            raise EmbeddingConfigError(
                "embedding api key is not configured; set EMBEDDING_API_KEY"
            )
        self.base_url = self.settings.embedding_base_url
        self.model = self.settings.embedding_model
        self.dimensions = max(1, int(self.settings.embedding_dimensions or 1024))
        self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    async def embed(
        self,
        texts: list[str],
        *,
        text_type: TextType,
    ) -> EmbeddingResult:
        clean = [str(text or "").strip() for text in texts]
        if not clean:
            return EmbeddingResult(vectors=[])
        kwargs = {
            "model": self.model,
            "input": clean,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        resp = await self._client.embeddings.create(**kwargs)
        ordered: list[list[float] | None] = [None] * len(clean)
        for idx, item in enumerate(resp.data):
            text_index = int(getattr(item, "index", idx))
            if 0 <= text_index < len(ordered):
                ordered[text_index] = [float(v) for v in item.embedding]
        vectors = [_normalize(vec or []) for vec in ordered]
        usage = getattr(resp, "usage", None)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        return EmbeddingResult(vectors=vectors, total_tokens=total_tokens)


def get_embedding_client(
    settings: Settings | None = None,
) -> DashScopeEmbeddingClient | OpenAICompatibleEmbeddingClient:
    settings = settings or get_settings()
    if settings.embedding_provider == "dashscope":
        return DashScopeEmbeddingClient(settings)
    if settings.embedding_provider == "openai-compatible":
        return OpenAICompatibleEmbeddingClient(settings)
    raise EmbeddingConfigError(f"unknown embedding provider: {settings.embedding_provider}")


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector
    return [v / norm for v in vector]
