from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, or_, select

from app.adapters.observation import Observer
from app.agent.tools.base import ToolContext, ToolDefinition, ToolResult
from app.agent.tools.registry import ToolRegistry
from app.api.common import fail
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import session
from app.rag.retrieval import RetrievalService, authorize, candidate, ready_statement
from app.services.runs import atomic_thread
from app.settings import Settings


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class ListDocumentsArgs(ToolArgs):
    kb_id: UUID | None = None
    keyword: str | None = None
    page: int = Field(1, ge=1, strict=True)
    page_size: int = Field(20, ge=1, le=50, strict=True)


class SearchKnowledgeArgs(ToolArgs):
    query: str = Field(min_length=1)
    kb_ids: list[UUID] | None = None


class ReadDocumentArgs(ToolArgs):
    document_ref: str = Field(pattern=r'^d[1-9][0-9]{0,14}$', description='本轮工具或导航返回的dN文档句柄')
    start_chunk: int = Field(0, ge=0, strict=True)
    limit: int = Field(5, ge=1, le=10, strict=True)


class ReadChunkArgs(ToolArgs):
    chunk_ref: str = Field(pattern=r'^c[1-9][0-9]{0,14}$', description='本轮工具或导航返回的cN片段句柄')
    before: int = Field(1, ge=0, le=4, strict=True, description='读取目标之前的片段数量')
    after: int = Field(1, ge=0, le=4, strict=True, description='读取目标之后的片段数量')


def scoped_kb_ids(context: ToolContext, requested: list[UUID] | None) -> list[str]:
    ids = context.kb_ids if requested is None else frozenset(str(k) for k in requested)
    if not ids.issubset(context.kb_ids):
        fail(403, 'permission_denied', '知识库超出本次运行授权范围')
    return sorted(ids)


def unique_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list({source.get('chunk_id', source['document_id']): source for source in sources}.values())


def document_title_expression():
    title = select(Chunk.source_meta['document_title'].as_string()).where(
        Chunk.document_id == Document.id, Chunk.kb_id == Document.kb_id
    ).order_by(Chunk.ordinal).limit(1).correlate(Document).scalar_subquery()
    return func.coalesce(func.nullif(title, ''), Document.original_name)


def _list_documents(context: ToolContext, ids: list[str], args: ListDocumentsArgs) -> ToolResult:
    authorize(context.user_id, ids)
    title = document_title_expression()
    statement = select(Document, KnowledgeBase.name, title).join(KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(
        KnowledgeBase.id.in_(ids), KnowledgeBase.owner_id == context.user_id, KnowledgeBase.status == 'active')
    ordering = []
    if args.keyword:
        names = (Document.original_name, title)
        statement = statement.where(or_(*(name.contains(args.keyword, autoescape=True) for name in names)))
        ordering.append(case((or_(*(name == args.keyword for name in names)), 0),
                             (or_(*(name.startswith(args.keyword, autoescape=True) for name in names)), 1), else_=2))
    with session() as db:
        total = db.scalar(select(func.count()).select_from(statement.subquery()))
        rows = db.execute(statement.order_by(*ordering, Document.created_at, Document.id)
                          .offset((args.page - 1) * args.page_size).limit(args.page_size)).all()
        documents = [{'document_id': doc.id, 'kb_id': doc.kb_id, 'kb_name': kb_name,
                      'document_title': doc_title, 'original_name': doc.original_name,
                      'parse_status': doc.parse_status, 'mime_type': doc.mime_type, 'size_bytes': doc.size_bytes}
                     for doc, kb_name, doc_title in rows]
    sources = [{'document_id': doc['document_id'], 'kb_id': doc['kb_id'],
                'original_name': doc['original_name']} for doc in documents]
    return ToolResult(ok=True, data={'documents': documents, 'page': args.page, 'page_size': args.page_size,
                                    'total': total, 'has_more': args.page * args.page_size < total},
                      sources=unique_sources(sources))


def _read_document(context: ToolContext, args: ReadDocumentArgs) -> ToolResult:
    document_id = context.refs.resolve(args.document_ref, 'd')
    with session() as db:
        doc = db.scalar(select(Document).join(KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(
            Document.id == document_id, KnowledgeBase.id.in_(sorted(context.kb_ids)),
            KnowledgeBase.owner_id == context.user_id, KnowledgeBase.status == 'active',
            Document.parse_status == 'ready'))
        if doc is None:
            fail(404, 'document_unavailable', '文档不存在、不可用或无访问权限')
        kb_name = db.scalar(select(KnowledgeBase.name).where(KnowledgeBase.id == doc.kb_id))
        doc_title = db.scalar(select(document_title_expression()).where(Document.id == doc.id))
        rows = db.execute(ready_statement(context.user_id, [doc.kb_id]).where(
            Document.id == doc.id, Chunk.ordinal >= args.start_chunk).order_by(Chunk.ordinal)
            .limit(args.limit + 1)).all()
        chunks = [{**candidate(chunk, document), 'ordinal': chunk.ordinal}
                  for chunk, document in rows[:args.limit]]
        for item in chunks:
            item['source']['kb_name'] = kb_name
        next_start = rows[args.limit][0].ordinal if len(rows) > args.limit else None
    return ToolResult(ok=True, data={'document_id': document_id, 'document_title': doc_title,
                                    'original_name': doc.original_name, 'kb_id': doc.kb_id, 'kb_name': kb_name,
                                    'start_chunk': args.start_chunk,
                                    'chunks': chunks, 'next_start': next_start},
                      sources=unique_sources([chunk['source'] for chunk in chunks]))


def _read_chunk(context: ToolContext, args: ReadChunkArgs) -> ToolResult:
    chunk_id = context.refs.resolve(args.chunk_ref, 'c')
    with session() as db:
        statement = ready_statement(context.user_id, sorted(context.kb_ids))
        target = db.execute(statement.where(Chunk.id == chunk_id)).first()
        if target is None:
            fail(404, 'chunk_unavailable', '片段不存在、不可用或无访问权限')
        chunk, document = target
        kb_name = db.scalar(select(KnowledgeBase.name).where(KnowledgeBase.id == document.kb_id))
        statement = statement.where(Document.id == document.id)
        before = db.execute(statement.where(Chunk.ordinal < chunk.ordinal)
            .order_by(Chunk.ordinal.desc()).limit(args.before)).all()
        after = db.execute(statement.where(Chunk.ordinal > chunk.ordinal)
            .order_by(Chunk.ordinal).limit(args.after)).all()
        chunks = [{**candidate(item, doc), 'ordinal': item.ordinal}
                  for item, doc in [*reversed(before), target, *after]]
        for item in chunks:
            item['source']['kb_name'] = kb_name
    return ToolResult(ok=True, data={'document_id': document.id, 'target_chunk_id': chunk.id,
                                    'chunks': chunks},
                      sources=unique_sources([item['source'] for item in chunks]))


class KnowledgeTools:
    def __init__(self, retrieval: RetrievalService, observer: Observer):
        self.retrieval = retrieval
        self.observer = observer

    async def list_documents(self, args: ListDocumentsArgs, context: ToolContext) -> ToolResult:
        requested = None if args.kb_id is None else [args.kb_id]
        return await atomic_thread(_list_documents, context, scoped_kb_ids(context, requested), args)

    async def search_knowledge(self, args: SearchKnowledgeArgs, context: ToolContext) -> ToolResult:
        ids = scoped_kb_ids(context, args.kb_ids)
        tracer = getattr(self.observer, 'tracer', None)
        if tracer is not None:
            result = await self.retrieval.search_knowledge(args.query, ids, context.user_id, self.observer, tracer)
        else:
            result = await self.retrieval.search_knowledge(args.query, ids, context.user_id, self.observer)
        names = await atomic_thread(_kb_names, context, ids)
        for hits in result.values():
            for hit in hits:
                hit['source']['kb_name'] = names[hit['source']['kb_id']]
        return ToolResult(ok=True, data=result, sources=unique_sources([hit['source'] for hit in result['reranked']]))

    async def read_document(self, args: ReadDocumentArgs, context: ToolContext) -> ToolResult:
        return await atomic_thread(_read_document, context, args)


    async def read_chunk(self, args: ReadChunkArgs, context: ToolContext) -> ToolResult:
        return await atomic_thread(_read_chunk, context, args)


def _kb_names(context: ToolContext, ids: list[str]) -> dict[str, str]:
    with session() as db:
        return dict(db.execute(select(KnowledgeBase.id, KnowledgeBase.name).where(
            KnowledgeBase.id.in_(ids), KnowledgeBase.owner_id == context.user_id,
            KnowledgeBase.status == 'active')).all())


def project_passage(item: dict, sources: dict) -> dict:
    source = item.get('source', sources.get(item['chunk_id'], {}))
    meta = source.get('source_meta') or {}
    return {**{key: item[key] for key in ('chunk_id', 'content', 'ordinal', 'rank', 'score') if key in item},
            **{key: source.get(key) for key in ('document_id', 'kb_id', 'kb_name', 'original_name', 'page_start', 'page_end')},
            'document_title': meta.get('document_title') or source.get('original_name'),
            'section': ' > '.join(meta['heading_path']) if meta.get('heading_path') else None}


def project_knowledge_result(name: str, result: ToolResult) -> ToolResult:
    if not result.ok:
        return result.model_copy(deep=True)
    if name == 'list_documents':
        data = {key: value for key, value in result.data.items() if key != 'documents'}
        data['documents'] = [{key: doc[key] for key in ('document_id', 'parse_status', 'document_title',
                             'original_name', 'kb_id', 'kb_name') if key in doc}
                             for doc in result.data['documents']]
    elif name == 'search_knowledge':
        sources = {source['chunk_id']: source for source in result.sources}
        data = {'hits': [project_passage(hit, sources) for hit in result.data['reranked']]}
    else:
        data = {key: value for key, value in result.data.items() if key != 'chunks'}
        sources = {source['chunk_id']: source for source in result.sources}
        data['chunks'] = [project_passage(chunk, sources) for chunk in result.data['chunks']]
    return ToolResult(ok=True, data=data, raw_result_ref=result.raw_result_ref)


def create_knowledge_registry(settings: Settings, retrieval: RetrievalService, observer: Observer) -> ToolRegistry:
    registry = ToolRegistry(settings, project_knowledge_result)
    handlers = KnowledgeTools(retrieval, observer)
    for definition in (
        ToolDefinition('list_documents', '按文件名和文档标题查找文档，或分页浏览授权知识库目录；返回dN句柄及处理状态，列表不是引用证据', ListDocumentsArgs, True, handlers.list_documents),
        ToolDefinition('search_knowledge', '检索授权知识库中的ready文档证据，同时返回cN片段句柄和所属dN文档句柄', SearchKnowledgeArgs, True, handlers.search_knowledge),
        ToolDefinition('read_document', '按块顺序分页读取已授权且ready的文档；next_start用于下一页', ReadDocumentArgs, True, handlers.read_document),
        ToolDefinition('read_chunk', '用本轮cN句柄读取目标片段及同文档前后片段，按原文顺序返回；before/after各默认1，范围0～4；用于补读条件、例外与表格上下文', ReadChunkArgs, True, handlers.read_chunk),
    ):
        registry.register(definition)
    return registry
