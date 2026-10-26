import shutil
import time

from rq.command import send_stop_job_command
from sqlalchemy import delete, select

from app.adapters.milvus import VectorStore
from app.api.common import bump_version, fail, owned_document, owned_kb
from app.db.models import Chunk, Document, KnowledgeBase
from app.db.session import session
from app.ingest.files import document_directory
from app.ingest.locks import document_lock
from app.ingest.queue import redis_connection
from app.ingest.recovery import job_for, live_execution
from app.ingest.writers import writer_alive
from app.settings import load_settings


def quiesce(document_id, job_id):
    job = job_for(job_id)
    if job:
        status = job.get_status(refresh=True).value
        if status in {'queued', 'deferred', 'scheduled'}:
            job.cancel()
        elif status == 'started':
            send_stop_job_command(redis_connection(), job.id)
    deadline = time.monotonic() + 10
    while writer_alive(document_id) or (job and (live_execution(job) or job.get_status(refresh=True).value == 'started')):
        if time.monotonic() >= deadline:
            fail(409, 'writer_active', '旧执行进程尚未停止，请稍后继续删除', True)
        time.sleep(0.2)


def delete_document(document_id, owner):
    with session() as db:
        doc = owned_document(db, document_id, owner, False)
        kb = owned_kb(db, doc.kb_id, owner, False)
        db.refresh(kb, with_for_update=True)
        db.refresh(doc, with_for_update=True)
        if doc.parse_status != 'deleting':
            doc.parse_status, doc.stage = 'deleting', 'delete'
            bump_version(db, kb.id)
        db.commit()
        job_id = doc.job_id
    try:
        quiesce(document_id, job_id)
        with document_lock(document_id) as lease:
            if writer_alive(document_id):
                fail(409, 'writer_active', '旧执行进程仍然存在', True)
            store = VectorStore(load_settings())
            try:
                store.remove('document_id', document_id)
            finally:
                store.close()
            lease.check()
            with session() as db:
                db.execute(delete(Chunk).where(Chunk.document_id == document_id))
                db.commit()
            directory = document_directory(document_id)
            if directory.exists():
                shutil.rmtree(directory)
            lease.check()
            with session() as db:
                doc = owned_document(db, document_id, owner, False)
                if doc.parse_status != 'deleting':
                    fail(409, 'state_conflict', '文档删除状态发生变化')
                db.delete(doc)
                db.commit()
    except Exception as error:
        with session() as db:
            doc = db.get(Document, document_id)
            if doc is not None and doc.parse_status == 'deleting':
                doc.error_code, doc.error_message = 'cleanup_failed', '清理尚未完成，请再次删除'
                db.commit()
        from fastapi import HTTPException
        if isinstance(error, HTTPException):
            raise
        fail(503, 'cleanup_failed', '文档清理失败，状态已保留，可再次删除', True)


def delete_kb(kb_id, owner):
    with session() as db:
        kb = owned_kb(db, kb_id, owner, False)
        db.refresh(kb, with_for_update=True)
        kb.status = 'deleting'
        db.commit()
        ids = list(db.scalars(select(Document.id).where(Document.kb_id == kb.id)))
        kb_id = kb.id
    for document_id in ids:
        try:
            delete_document(document_id, owner)
        except Exception:
            fail(503, 'kb_cleanup_failed', '知识库清理未完成，失败文档：'+document_id, True)
    store = VectorStore(load_settings())
    try:
        store.remove('kb_id', kb_id)
    except Exception:
        fail(503, 'kb_vectors_remaining', '知识库残留向量清理失败，请再次删除', True)
    finally:
        store.close()
    with session() as db:
        kb = owned_kb(db, kb_id, owner, False)
        db.delete(kb)
        db.commit()
