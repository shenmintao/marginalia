from __future__ import annotations

from dataclasses import dataclass

import httpx

from marginalia.config import Settings, get_settings
from marginalia.model_rate_limit import acquire_model_call_slot
from marginalia.provider_http import raise_for_provider_status


@dataclass(slots=True)
class RerankHit:
    index: int
    score: float
    rank: int


class RerankConfigError(RuntimeError):
    pass


def rerank_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(settings.rerank_enabled and settings.rerank_api_key)


class BailianRerankClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = self.settings.rerank_api_key
        if not self.api_key:
            raise RerankConfigError("rerank api key is not configured; set RERANK_API_KEY")
        self.model = self.settings.rerank_model
        self.endpoint = _rerank_endpoint(self.settings.rerank_base_url)

    async def rerank(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankHit]:
        clean = [str(document or "").strip() for document in documents]
        if not query.strip() or not clean:
            return []
        effective_top_n = max(1, min(len(clean), int(top_n or len(clean))))
        hits: list[RerankHit] = []
        batch_size = max(1, int(self.settings.rerank_batch_size or 1))
        for offset in range(0, len(clean), batch_size):
            batch = clean[offset:offset + batch_size]
            batch_top_n = max(1, min(len(batch), effective_top_n))
            batch_hits = await self._rerank_batch(query, batch, top_n=batch_top_n)
            hits.extend(
                RerankHit(index=hit.index + offset, score=hit.score, rank=0)
                for hit in batch_hits
            )
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return [
            RerankHit(index=hit.index, score=hit.score, rank=rank)
            for rank, hit in enumerate(hits[:effective_top_n], start=1)
        ]

    async def _rerank_batch(
        self,
        query: str,
        documents: list[str],
        *,
        top_n: int,
    ) -> list[RerankHit]:
        payload = {
            "model": self.model,
            "input": {
                "query": query,
                "documents": documents,
            },
            "parameters": {
                "top_n": top_n,
            },
        } if _is_native_rerank_endpoint(self.endpoint) else {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": False,
            "instruct": (
                "Given a scientific or knowledge-base question, rank documents "
                "by usefulness as evidence for answering it."
            ),
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        await acquire_model_call_slot(
            kind="rerank",
            provider="bailian",
            base_url=self.settings.rerank_base_url,
            model=self.model,
            tps=self.settings.rerank_tps,
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.endpoint, headers=headers, json=payload)
            raise_for_provider_status(resp, "rerank")
        return _parse_rerank_hits(resp.json())


def get_rerank_client(settings: Settings | None = None) -> BailianRerankClient:
    return BailianRerankClient(settings)


def _rerank_endpoint(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/reranks") or _is_native_rerank_endpoint(base):
        return base
    return f"{base}/reranks"


def _is_native_rerank_endpoint(url: str) -> bool:
    path = str(url or "").rstrip("/").lower()
    return "/api/v1/services/rerank/" in path


def _parse_rerank_hits(obj: object) -> list[RerankHit]:
    if not isinstance(obj, dict):
        return []
    raw_results = obj.get("results")
    if not isinstance(raw_results, list):
        output = obj.get("output")
        if isinstance(output, dict):
            raw_results = output.get("results")
    if not isinstance(raw_results, list):
        return []

    hits: list[RerankHit] = []
    for rank, item in enumerate(raw_results, start=1):
        if not isinstance(item, dict):
            continue
        raw_index = item.get("index")
        if raw_index is None:
            continue
        try:
            index = int(raw_index)
            score = float(item.get("relevance_score", item.get("score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        if index >= 0:
            hits.append(RerankHit(index=index, score=score, rank=rank))
    return hits
