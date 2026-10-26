import asyncio
import re
import threading
from contextlib import nullcontext
from dataclasses import dataclass

import jieba
from rank_bm25 import BM25Okapi
from sqlalchemy import func, select

from app.api.common import fail, identifier
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import session
from app.settings import Settings
from app.adapters.common import AdapterError
from app.adapters.embedding import EmbeddingAdapter
from app.adapters.milvus import VectorStore
from app.adapters.rerank import RerankAdapter
from app.rag.fusion import fuse_rrf
from app.rag.tracing import NullSpan


def retrieval_output(hits, score_type):
    keys = ('chunk_id', 'rrf_score', 'bm25_rank', 'vector_rank', 'bm25_contribution', 'vector_contribution') if score_type == 'rrf' else ('chunk_id', 'score')
    return {'count': len(hits), 'score_type': score_type, 'hits': [
        {'rank': rank, **{key: hit[key] for key in keys if key in hit},
         **{key: hit.get('source', {}).get(key) for key in ('document_id', 'kb_id', 'original_name')}}
        for rank, hit in enumerate(hits, 1)]}


def tokenize(text: str) -> list[str]:
    return [token.casefold() for token in jieba.lcut(text.strip()) if re.search(r'\w', token)]


def clean_query(query: str) -> str:
    query = query.strip()
    if not query or not tokenize(query):
        fail(422, 'empty_query', '查询必须包含有效文字')
    return query


def authorize(owner: str, kb_ids: list[str]) -> list[tuple[str, int]]:
    ids = sorted({identifier(k) for k in kb_ids})
    if not ids:
        return []
    with session() as db:
        rows = db.execute(select(KnowledgeBase.id, KnowledgeBase.index_version).where(
            KnowledgeBase.id.in_(ids), KnowledgeBase.owner_id == owner,
            KnowledgeBase.status == 'active')).all()
        if len(rows) != len(ids):
            fail(404, 'kb_unavailable', '知识库不存在、不可用或无访问权限')
        return sorted((k, v) for k, v in rows)


def ready_statement(owner: str, kb_ids: list[str]):
    return select(Chunk, Document).join(Document, Chunk.document_id == Document.id).join(
        KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(
        Chunk.kb_id == Document.kb_id, KnowledgeBase.id.in_(kb_ids),
        KnowledgeBase.owner_id == owner, KnowledgeBase.status == 'active',
        Document.parse_status == 'ready')


def candidate(chunk: Chunk, doc: Document) -> dict:
    return {'chunk_id': chunk.id, 'content': chunk.content, 'source': {
        'chunk_id': chunk.id, 'document_id': doc.id, 'kb_id': chunk.kb_id,
        'original_name': doc.original_name, 'page_start': chunk.page_start,
        'page_end': chunk.page_end, 'title': chunk.title, 'source_meta': chunk.source_meta,
        'download_url': f'/api/documents/{doc.id}/download'}}


def hydrate(owner: str, kb_ids: list[str], hits: list[dict]) -> list[dict]:
    kb_ids = [kb_id for kb_id, _ in authorize(owner, kb_ids)]
    if not hits:
        return []
    with session() as db:
        rows = db.execute(ready_statement(owner, kb_ids).where(Chunk.id.in_([h['chunk_id'] for h in hits]))).all()
        current = {c.id: candidate(c, d) for c, d in rows}
    return [{**hit, **current[hit['chunk_id']]} for hit in hits if hit['chunk_id'] in current]


@dataclass
class Corpus:
    rows: list[dict]
    model: BM25Okapi | None


class BM25Cache:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.entries: dict[tuple[str, int], Corpus] = {}
        self.lock = threading.RLock()

    def load(self, owner: str, kb_id: str, version: int) -> Corpus:
        key = (kb_id, version)
        with self.lock:
            if key in self.entries:
                return self.entries[key]
            with session() as db:
                statement = ready_statement(owner, [kb_id])
                count = db.scalar(select(func.count()).select_from(statement.subquery()))
                if count > self.settings.BM25_MAX_CHUNKS_PER_KB:
                    fail(422, 'bm25_chunk_limit', f'每库演示上限{self.settings.BM25_MAX_CHUNKS_PER_KB}块；当前{count}块，未截断搜索')
                rows = [candidate(c, d) for c, d in db.execute(statement.order_by(Chunk.id)).all()]
            tokens = [tokenize(row['content']) for row in rows]
            model = BM25Okapi(tokens) if tokens and any(tokens) else None
            corpus = Corpus(rows, model)
            self.entries = {k: v for k, v in self.entries.items() if k[0] != kb_id}
            self.entries[key] = corpus
            return corpus

    def search(self, owner: str, query: str, kb_ids: list[str], limit: int) -> list[dict]:
        tokens = tokenize(clean_query(query))
        for _ in range(3):
            versions = authorize(owner, kb_ids)
            hits = []
            for kb_id, version in versions:
                corpus = self.load(owner, kb_id, version)
                if corpus.model is not None:
                    scores = corpus.model.get_scores(tokens)
                    hits.extend({'chunk_id': row['chunk_id'], 'score': float(score)}
                                for row, score in zip(corpus.rows, scores) if score > 0)
            if versions == authorize(owner, kb_ids):
                hits.sort(key=lambda h: (-h['score'], h['chunk_id']))
                selected = []
                if limit <= 0:
                    selected = hydrate(owner, kb_ids, hits)
                else:
                    for offset in range(0, len(hits), limit):
                        selected.extend(hydrate(owner, kb_ids, hits[offset:offset + limit]))
                        if len(selected) >= limit:
                            break
                return [{**h, 'rank': i} for i, h in enumerate(selected[:limit], 1)]
        fail(503, 'index_changing', '索引正在变更，请稍后重试', True)


class RetrievalService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = BM25Cache(settings)

    async def bm25_search(self, query: str, kb_ids: list[str], owner: str, limit: int = 20):
        return await asyncio.to_thread(self.cache.search, owner, query, kb_ids, limit)

    def vector_candidates(self, owner: str, kb_ids: list[str], embedding) -> list[dict]:
        authorize(owner, kb_ids)
        with session() as db:
            ids = sorted(db.scalars(ready_statement(owner, kb_ids).with_only_columns(Document.id).distinct()))
        if not ids:
            return []
        store = VectorStore(self.settings)
        try:
            store.ensure(embedding.dimension)
            # UUID均来自授权MySQL行；过滤ready文档，避免失效残留向量占满Top20。
            kb_filter = ','.join('"'+identifier(k)+'"' for k in kb_ids)
            doc_filter = ','.join('"'+identifier(d)+'"' for d in ids)
            hits = store.client.search(store.name, data=[embedding.vectors[0]],
                filter=f'kb_id in [{kb_filter}] and document_id in [{doc_filter}]',
                limit=self.settings.RETRIEVAL_ROUTE_LIMIT,
                search_params={'metric_type': 'COSINE', 'params': {'ef': 64}},
                consistency_level='Strong', timeout=self.settings.VECTOR_TIMEOUT)[0]
            rows = [{'chunk_id': hit.get('chunk_id', hit.get('id')), 'score': float(hit['distance'])} for hit in hits]
        except AdapterError:
            raise
        except Exception:
            raise AdapterError('vector_search_failed', True, 'Milvus检索失败', None) from None
        finally:
            store.close()
        rows = hydrate(owner, kb_ids, rows)
        rows.sort(key=lambda h: (-h['score'], h['chunk_id']))
        return [{**h, 'rank': i} for i, h in enumerate(rows, 1)]

    async def vector_search(self, query: str, kb_ids: list[str], owner: str, observer):
        with_ready = await asyncio.to_thread(self.has_ready, owner, kb_ids)
        if not with_ready:
            return []
        adapter = EmbeddingAdapter(self.settings, observer)
        try:
            embedding = await adapter.embed([query])
        finally:
            await adapter.close()
        tracer = getattr(observer, 'tracer', None)
        stage = tracer.stage if tracer else lambda *args, **kwargs: nullcontext(NullSpan())
        with stage('vector.search', {'kb_ids': kb_ids, 'dimension': embedding.dimension}) as span:
            hits = await asyncio.to_thread(self.vector_candidates, owner, kb_ids, embedding)
            span.update(output=retrieval_output(hits, 'cosine'))
            return hits

    @staticmethod
    def has_ready(owner, kb_ids):
        authorize(owner, kb_ids)
        with session() as db:
            return db.scalar(ready_statement(owner, kb_ids).with_only_columns(Chunk.id).limit(1)) is not None

    async def rerank(self, query: str, candidates: list[dict], observer) -> list[dict]:
        if not candidates:
            return []
        adapter = RerankAdapter(self.settings, observer)
        try:
            ranks = await adapter.rerank(query, [c['content'] for c in candidates])
        finally:
            await adapter.close()
        mapped = [{**candidates[r['index']], 'score': r['score']} for r in ranks]
        mapped.sort(key=lambda h: (-h['score'], h['chunk_id']))
        return [{**h, 'rank': i} for i, h in enumerate(mapped[:self.settings.RETRIEVAL_FINAL_LIMIT], 1)]

    async def search_knowledge(self, query: str, kb_ids: list[str], owner: str, observer, tracer=None) -> dict:
        query = await asyncio.to_thread(clean_query, query)
        versions = await asyncio.to_thread(authorize, owner, kb_ids)
        kb_ids = [kb_id for kb_id, _ in versions]
        async def check_versions():
            if versions != await asyncio.to_thread(authorize, owner, kb_ids):
                raise AdapterError('index_changing', True, '检索期间索引已变化，请重新检索', None)
        # 无追踪时也使用同一服务；阶段上下文由问答模式提供。
        stage = tracer.stage if tracer else lambda *args, **kwargs: nullcontext(NullSpan())
        async def route(name, operation, score_type):
            with stage(name, {'query': query, 'kb_ids': kb_ids}) as span:
                hits = await operation
                span.update(output=retrieval_output(hits, score_type))
                return hits

        with stage('retrieve', {'query': query, 'kb_ids': kb_ids}, as_type='retriever') as span:
            bm25, vector = await asyncio.gather(
                route('bm25', self.bm25_search(query, kb_ids, owner, self.settings.RETRIEVAL_ROUTE_LIMIT), 'bm25'),
                route('vector', self.vector_search(query, kb_ids, owner, observer), 'cosine'))
            candidate_ids = dict.fromkeys(h['chunk_id'] for h in bm25 + vector)
            current = {h['chunk_id']: h for h in await asyncio.to_thread(
                hydrate, owner, kb_ids, [{'chunk_id': cid} for cid in candidate_ids])}
            bm25 = [{**h, **current[h['chunk_id']]} for h in bm25 if h['chunk_id'] in current]
            vector = [{**h, **current[h['chunk_id']]} for h in vector if h['chunk_id'] in current]
            bm25 = [{**h, 'rank': i} for i, h in enumerate(bm25, 1)]
            vector = [{**h, 'rank': i} for i, h in enumerate(vector, 1)]
            await check_versions()
            with stage('fusion', {'bm25_count': len(bm25), 'vector_count': len(vector),
                       'rrf_k': self.settings.RETRIEVAL_RRF_K}) as fusion_span:
                fused = fuse_rrf([bm25, vector], self.settings.RETRIEVAL_RRF_K)[:self.settings.RETRIEVAL_FUSED_LIMIT]
                fusion_span.update(output=retrieval_output(fused, 'rrf'))
            span.update(output={'bm25_count': len(bm25), 'vector_count': len(vector), 'fused_count': len(fused)})
        with stage('rerank', {'query': query, 'chunk_ids': [c['chunk_id'] for c in fused]}) as span:
            reranked = await self.rerank(query, fused, observer)
            reranked = await asyncio.to_thread(hydrate, owner, kb_ids, reranked)
            reranked = [{**h, 'rank': i} for i, h in enumerate(reranked, 1)]
            await check_versions()
            span.update(output=retrieval_output(reranked, 'rerank'), metadata={'skipped': not fused})
        return {'bm25': bm25, 'vector': vector, 'fused': fused, 'reranked': reranked}
