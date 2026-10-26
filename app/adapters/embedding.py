import math
from typing import Any

from openai import AsyncOpenAI

from app.adapters.common import AdapterError, EmbeddingResult, normalize_error, parse_usage, request_id
from app.adapters.observation import Observer
from app.settings import Settings


def validate_vectors(data: list[Any], count: int, dimension: int | None) -> list[list[float]]:
    if len(data) != count or {item.index for item in data} != set(range(count)):
        raise AdapterError("invalid_embedding_indexes", False, "Embedding 数量或 index 不匹配", None)
    vectors = [item.embedding for item in sorted(data, key=lambda item: item.index)]
    sizes = {len(vector) for vector in vectors}
    if not vectors or len(sizes) != 1 or 0 in sizes or any(not math.isfinite(value) for vector in vectors for value in vector):
        raise AdapterError("invalid_embedding_vectors", False, "Embedding 向量非有限值、空向量或维度不一致", None)
    if dimension is not None and sizes != {dimension}:
        raise AdapterError("embedding_dimension_mismatch", False, "Embedding 实际维度与配置不符", None)
    return vectors


class EmbeddingAdapter:
    def __init__(self, settings: Settings, observer: Observer):
        settings.require("EMBED_BASE_URL", "EMBED_API_KEY", "EMBED_MODEL")
        self.model, self.dimension, self.observer = settings.EMBED_MODEL, settings.EMBED_DIMENSION, observer
        self.client = AsyncOpenAI(base_url=settings.EMBED_BASE_URL, api_key=settings.EMBED_API_KEY.get_secret_value(), max_retries=0, timeout=settings.timeout("EMBED", True))

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        provider_id = None
        with self.observer.observe("embedding", self.model, {'count': len(texts), 'characters': sum(map(len, texts))}) as observation:
            try:
                raw = await self.client.embeddings.with_raw_response.create(model=self.model, input=texts, encoding_format="float")
                provider_id = request_id(raw.headers)
                response = raw.parse()
                if response.model != self.model:
                    raise AdapterError('embedding_model_mismatch', False, 'Embedding响应模型与配置不一致', provider_id)
                vectors = validate_vectors(response.data, len(texts), self.dimension)
                result = EmbeddingResult(vectors=vectors, dimension=len(vectors[0]), model=response.model, usage=parse_usage(response.usage), provider_request_id=provider_id)
                observation.usage, observation.provider_request_id = result.usage, provider_id
                observation.output = {"dimension": result.dimension, "count": len(vectors)}
                return result
            except Exception as exc:
                raise normalize_error(exc, self.observer.sanitizer, provider_id) from None

    async def close(self) -> None:
        await self.client.close()
