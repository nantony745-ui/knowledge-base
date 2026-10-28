import asyncio
from copy import copy

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.adapters.embedding import EmbeddingAdapter
from app.adapters.milvus import VectorStore
from app.adapters.observation import Observer
from app.api.auth import current_user, require_origin
from app.api.common import owned_kb
from app.api.schemas import SearchHit, SearchInput
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import session
from app.settings import load_settings

router = APIRouter(prefix='/api/knowledge-bases', tags=['语义检索'])
debug_router = APIRouter(prefix='/api/search', tags=['双路检索'])
chunks_router = APIRouter(prefix='/api/chunks', tags=['来源片段'])


class DebugInput(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    kb_ids: list[str] = Field(min_length=1, max_length=20)


@chunks_router.get('/{chunk_id}')
def get_chunk(chunk_id: str, owner: str = Depends(current_user)):
    from app.api.common import fail, identifier
    from app.rag.retrieval import candidate
    with session() as db:
        row = db.execute(select(Chunk,Document).join(Document,Chunk.document_id==Document.id)
            .join(KnowledgeBase,Document.kb_id==KnowledgeBase.id).where(
                Chunk.id==identifier(chunk_id),Chunk.kb_id==Document.kb_id,
                KnowledgeBase.owner_id==owner,KnowledgeBase.status=='active',Document.parse_status=='ready')).first()
        if row is None:
            fail(404,'chunk_unavailable','片段不存在、不可用或无访问权限')
        return candidate(*row)


@debug_router.post('/debug', dependencies=[Depends(require_origin)])
async def debug_search(body: DebugInput, request: Request, owner: str = Depends(current_user)):
    from app.rag.retrieval import RetrievalService
    if not hasattr(request.app.state, 'retrieval'):
        request.app.state.retrieval = RetrievalService(load_settings())
    settings = await asyncio.to_thread(load_settings)
    retrieval = copy(request.app.state.retrieval)
    retrieval.settings = settings
    observer = Observer(settings)
    result = await retrieval.search_knowledge(body.query, body.kb_ids, owner, observer)
    return {
        'bm25': [{k: h[k] for k in ('chunk_id', 'rank', 'score')} for h in result['bm25']],
        'vector': [{k: h[k] for k in ('chunk_id', 'rank', 'score')} for h in result['vector']],
        'fused': [{k: h[k] for k in ('chunk_id', 'rrf_score', 'bm25_rank', 'vector_rank',
                                    'bm25_contribution', 'vector_contribution')} for h in result['fused']],
        'reranked': [{k: h[k] for k in ('chunk_id', 'rank', 'score', 'content', 'source')} for h in result['reranked']],
    }


async def query_vector(query: str):
    settings = load_settings()
    observer = Observer(settings)
    adapter = EmbeddingAdapter(settings, observer)
    try:
        return await adapter.embed([query])
    finally:
        await adapter.close()


@router.post('/{kb_id}/search', response_model=list[SearchHit], dependencies=[Depends(require_origin)])
def search(kb_id: str, body: SearchInput, owner: str = Depends(current_user)):
    with session() as db:
        kb_id = owned_kb(db, kb_id, owner, True).id
    result = asyncio.run(query_vector(body.query))
    store = VectorStore(load_settings())
    try:
        store.ensure(result.dimension)
        hits = store.search(kb_id, result.vectors[0], min(200, max(40, body.top_k * 10)))
    finally:
        store.close()
    with session() as db:
        owned_kb(db, kb_id, owner, True)
        rows = db.execute(select(Chunk, Document).join(Document, Document.id == Chunk.document_id)
                          .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                          .where(Chunk.id.in_([hit['chunk_id'] for hit in hits]), Chunk.kb_id == kb_id,
                                 Document.kb_id == kb_id, Document.parse_status == 'ready',
                                 KnowledgeBase.owner_id == owner, KnowledgeBase.status == 'active')).all()
        authoritative = {chunk.id: (chunk, doc) for chunk, doc in rows}
        output = []
        for hit in hits:
            if hit['chunk_id'] not in authoritative:
                continue
            chunk, doc = authoritative[hit['chunk_id']]
            output.append(SearchHit(chunk_id=chunk.id, document_id=doc.id, kb_id=kb_id,
                                     document_name=doc.original_name, content=chunk.content, title=chunk.title,
                                     score=hit['score'], page_start=chunk.page_start, page_end=chunk.page_end,
                                     source_meta=chunk.source_meta, download_url=f'/api/documents/{doc.id}/download'))
            if len(output) >= body.top_k:
                break
        return output
