from __future__ import annotations

from dataclasses import dataclass

import httpx

from marginalia.config import Settings, get_settings


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
        payload = {
            "model": self.model,
            "query": query,
            "documents": clean,
            "top_n": max(1, min(len(clean), int(top_n or len(clean)))),
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
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(self.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
        return _parse_rerank_hits(resp.json())


def get_rerank_client(settings: Settings | None = None) -> BailianRerankClient:
    return BailianRerankClient(settings)


def _rerank_endpoint(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/reranks"):
        return base
    return f"{base}/reranks"


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
