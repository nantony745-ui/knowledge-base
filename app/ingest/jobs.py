import asyncio
import json
import logging
from time import monotonic
from uuid import uuid4

from sqlalchemy import delete, update

from app.adapters.common import AdapterError
from app.adapters.embedding import EmbeddingAdapter
from app.adapters.milvus import VectorStore
from app.adapters.observation import Observer
from app.api.common import bump_version
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import engine, session
from app.ingest.chunker import chunk_text
from app.ingest.files import stored_file, document_directory
from app.ingest.locks import document_lock
from app.ingest.parsers import parse_text
from app.settings import load_settings
from app.ingest.writers import register_writer, unregister_writer

logger = logging.getLogger('personal_knora.ingest')


class ObsoleteJob(RuntimeError):
    pass


def running_document(db, document_id, job_id):
    doc = db.get(Document, document_id)
    if doc is None:
        raise ObsoleteJob('文档已删除')
    kb = db.get(KnowledgeBase, doc.kb_id, with_for_update=True)
    db.refresh(doc, with_for_update=True)
    if kb is None or kb.status != 'active' or doc.job_id != job_id or doc.parse_status not in {'queued', 'parsing', 'indexing'}:
        raise ObsoleteJob('当前执行已失效')
    return doc


def stage(document_id, job_id, status, name, lease):
    lease.check()
    with session() as db:
        doc = running_document(db, document_id, job_id)
        doc.parse_status, doc.stage = status, name
        db.commit()
        logger.warning('document=%s job=%s stage=%s', document_id, job_id, name)
        return doc


def parsed_text(doc, job_id, lease):
    if doc.parsed_path:
        path = stored_file(doc.parsed_path, doc.id)
        if path.is_file():
            return parse_text(path)
    text = parse_text(stored_file(doc.original_path, doc.id))
    directory = document_directory(doc.id) / 'parsed'
    directory.mkdir(exist_ok=True)
    temporary = directory / f'{job_id}.tmp'
    temporary.write_text(text, encoding='utf-8')
    lease.check()
    with session() as db:
        current = running_document(db, doc.id, job_id)
        temporary.replace(directory / 'text.md')
        current.parsed_path = f'documents/{doc.id}/parsed/text.md'
        db.commit()
    return text


def save_chunks(doc, job_id, pieces, lease):
    lease.check()
    with session() as db:
        running_document(db, doc.id, job_id)
        db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        chunks = [Chunk(id=str(uuid4()), kb_id=doc.kb_id, document_id=doc.id, ordinal=i,
                        content=p.content, title=(p.title or '')[:255] or None, source_meta=p.source_meta,
                        page_start=None, page_end=None) for i, p in enumerate(pieces)]
        db.add_all(chunks)
        db.commit()
        return chunks


async def index_document(document_id, job_id, lease):
    settings = load_settings()
    observer = Observer(settings)
    adapter = EmbeddingAdapter(settings, observer)
    store = None
    try:
        doc = await asyncio.to_thread(stage, document_id, job_id, 'parsing', 'parse', lease)
        text = await asyncio.to_thread(parsed_text, doc, job_id, lease)
        doc = await asyncio.to_thread(stage, document_id, job_id, 'indexing', 'chunk', lease)
        pieces = chunk_text(text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP)
        if not pieces:
            raise AdapterError('empty_chunks', False, '文本未生成有效片段', None)
        store = await asyncio.to_thread(VectorStore, settings)
        await asyncio.to_thread(stage, document_id, job_id, 'indexing', 'embed', lease)
        probe = await adapter.embed([pieces[0].content])
        lease.check()
        await asyncio.to_thread(store.ensure, probe.dimension)
        await asyncio.to_thread(store.remove, 'document_id', document_id)
        chunks = await asyncio.to_thread(save_chunks, doc, job_id, pieces, lease)
        for start in range(0, len(chunks), settings.EMBED_BATCH_SIZE):
            batch = chunks[start:start+settings.EMBED_BATCH_SIZE]
            await asyncio.to_thread(stage, document_id, job_id, 'indexing', 'embed', lease)
            texts = [json.dumps({'title': c.source_meta['document_title'], 'status': c.source_meta['document_status'], 'heading': c.source_meta['heading_path']}, ensure_ascii=False)+'\n'+c.content for c in batch]
            vectors = await adapter.embed(texts)
            if vectors.dimension != probe.dimension:
                raise AdapterError('embedding_dimension_changed', False, 'Embedding维度在索引中发生变化', None)
            await asyncio.to_thread(stage, document_id, job_id, 'indexing', 'vector_write', lease)
            rows = [{'chunk_id': c.id, 'kb_id': c.kb_id, 'document_id': c.document_id, 'embedding': v} for c, v in zip(batch, vectors.vectors)]
            await asyncio.to_thread(store.write, rows)
        await asyncio.to_thread(stage, document_id, job_id, 'indexing', 'verify', lease)
        await asyncio.to_thread(store.verify, document_id, {c.id for c in chunks}, probe.dimension)
        await asyncio.to_thread(complete, document_id, job_id, lease)
    finally:
        if store is not None:
            await asyncio.to_thread(store.close)
        await adapter.close()
        await asyncio.to_thread(observer.client.shutdown)


def complete(document_id, job_id, lease):
    lease.check()
    with session() as db:
        doc = running_document(db, document_id, job_id)
        doc.parse_status, doc.stage = 'ready', 'complete'
        doc.error_code = doc.error_message = None
        bump_version(db, doc.kb_id)
        db.commit()


def mark_failed(document_id, job_id, code, message):
    with session() as db:
        db.execute(update(Document).where(Document.id == document_id, Document.job_id == job_id,
                   Document.parse_status.in_(['queued', 'parsing', 'indexing'])).values(
                   parse_status='failed', error_code=code, error_message=message))
        db.commit()


def ingest_document(document_id: str, job_id: str):
    engine.cache_clear()
    started = monotonic()
    try:
        with document_lock(document_id) as lease:
            try:
                with session() as db:
                    running_document(db, document_id, job_id)
                    register_writer(document_id, job_id)
                asyncio.run(index_document(document_id, job_id, lease))
            finally:
                unregister_writer(document_id, job_id)
    except ObsoleteJob:
        logger.warning('obsolete document=%s job=%s', document_id, job_id)
    except Exception as error:
        code = error.info.code if isinstance(error, AdapterError) else 'indexing_failed'
        message = error.info.message if isinstance(error, AdapterError) else '任务处理失败：'+type(error).__name__
        mark_failed(document_id, job_id, code, message)
        logger.warning('failed document=%s job=%s code=%s', document_id, job_id, code)
        raise RuntimeError(code) from None
    finally:
        logger.warning('finished document=%s job=%s seconds=%.3f', document_id, job_id, monotonic()-started)
