from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.common import AdapterError, RankItem, normalize_error, parse_usage, request_id
from app.adapters.observation import Observer
from app.settings import Settings


class ProviderRank(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, strict=True)
    index: int = Field(ge=0)
    relevance_score: float


class RerankResponse(BaseModel):
    results: list[ProviderRank]
    meta: dict[str, Any] | None = None


def validate_ranking(data: dict[str, Any], count: int) -> tuple[list[dict[str, Any]], RerankResponse]:
    response = RerankResponse.model_validate(data)
    indexes = [item.index for item in response.results]
    if len(indexes) != count or len(set(indexes)) != count or any(index >= count for index in indexes):
        raise AdapterError("invalid_rerank_indexes", False, "Rerank index 越界、重复或数量不匹配", None)
    return [RankItem(index=item.index, score=item.relevance_score).model_dump() for item in response.results], response


class RerankAdapter:
    def __init__(self, settings: Settings, observer: Observer):
        settings.require("RERANK_URL", "RERANK_API_KEY", "RERANK_MODEL")
        self.url, self.model, self.observer = settings.RERANK_URL, settings.RERANK_MODEL, observer
        self.client = httpx.AsyncClient(timeout=settings.timeout("RERANK", False), headers={"Authorization": "Bearer " + settings.RERANK_API_KEY.get_secret_value()})

    async def rerank(self, query: str, documents: list[str]) -> list[dict[str, Any]]:
        provider_id = None
        with self.observer.observe("rerank", self.model, {"query": query, "documents": documents}) as observation:
            try:
                response = await self.client.post(self.url, json={"model": self.model, "query": query, "documents": documents, "top_n": len(documents), "return_documents": False})
                provider_id = request_id(response.headers)
                response.raise_for_status()
                ranks, parsed = validate_ranking(response.json(), len(documents))
                observation.usage = parse_usage(parsed.meta.get("tokens")) if parsed.meta else None
                observation.output, observation.provider_request_id = ranks, provider_id
                return ranks
            except Exception as exc:
                raise normalize_error(exc, self.observer.sanitizer, provider_id) from None

    async def close(self) -> None:
        await self.client.aclose()
