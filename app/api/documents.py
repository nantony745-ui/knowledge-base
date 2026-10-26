import asyncio
import codecs
import hashlib
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, update

from app.api.auth import current_user, require_origin
from app.api.common import bump_version, fail, owned_document, owned_kb
from app.api.schemas import DocumentOut, Queued
from app.db.models import Document
from app.db.session import session
from app.ingest.files import document_directory, stored_file
from app.ingest.queue import enqueue
from app.settings import load_settings

router = APIRouter(prefix='/api', tags=['文档'])


def enqueue_saved(document_id: str, job_id: str):
    try:
        enqueue(document_id, job_id)
    except Exception:
        with session() as db:
            db.execute(update(Document).where(Document.id == document_id, Document.job_id == job_id,
                       Document.parse_status == 'queued').values(parse_status='failed', stage='enqueue',
                       error_code='enqueue_failed', error_message='任务入队失败，请重试'))
            db.commit()
        fail(503, 'enqueue_failed', '文档已保存，但任务入队失败，可重试', True)
    return Queued(document_id=document_id, job_id=job_id, status='queued')


def register_document(kb_id, owner, document_id, name, extension, count, digest):
    with session() as db:
        kb = owned_kb(db, kb_id, owner, True)
        db.refresh(kb, with_for_update=True)
        owned_kb(db, kb_id, owner, True)
        job_id = str(uuid4())
        doc = Document(id=document_id, kb_id=kb.id, original_name=name,
                       mime_type='text/markdown' if extension == '.md' else 'text/plain', size_bytes=count,
                       sha256=digest, original_path=f'documents/{document_id}/original{extension}',
                       parse_status='queued', stage='enqueue', job_id=job_id)
        db.add(doc)
        db.commit()
    return job_id


async def save_upload(file, path, limit):
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
    digest = hashlib.sha256()
    count, nonempty = 0, False
    with path.open('xb') as output:
        while data := await file.read(64 * 1024):
            count += len(data)
            if count > limit:
                fail(413, 'file_too_large', '文件超过20 MiB上限')
            try:
                decoded = decoder.decode(data)
            except UnicodeDecodeError:
                fail(415, 'invalid_encoding', '文件必须为UTF-8或UTF-8 BOM编码')
            if '\x00' in decoded:
                fail(415, 'binary_content', '文本文件包含二进制内容')
            nonempty |= bool(decoded.strip())
            digest.update(data)
            output.write(data)
        try:
            nonempty |= bool(decoder.decode(b'', final=True).strip())
        except UnicodeDecodeError:
            fail(415, 'invalid_encoding', '文件包含不完整的UTF-8字符')
    if not count or not nonempty:
        fail(422, 'empty_document', '文档正文为空')
    return count, digest.hexdigest()


@router.post('/knowledge-bases/{kb_id}/documents', response_model=Queued, status_code=202, dependencies=[Depends(require_origin)])
async def upload(kb_id: str, file: UploadFile = File(...), owner: str = Depends(current_user)):
    name = Path((file.filename or '').replace('\\', '/')).name
    extension = Path(name).suffix.lower()
    if extension not in {'.txt', '.md'}:
        fail(415, 'unsupported_type', '本阶段仅支持TXT和MD')
    if not name or len(name) > 255:
        fail(422, 'invalid_filename', '文件名不能为空或超过255字符')
    if file.content_type and not (file.content_type.startswith('text/') or file.content_type in {'application/octet-stream', 'application/markdown'}):
        fail(415, 'mime_mismatch', '文件MIME类型与文本不一致')
    await asyncio.to_thread(check_active, kb_id, owner)
    document_id = str(uuid4())
    directory = document_directory(document_id)
    directory.mkdir(parents=True)
    registration = None
    try:
        count, digest = await save_upload(file, directory / ('original'+extension), load_settings().MAX_UPLOAD_BYTES)
        registration = asyncio.create_task(asyncio.to_thread(register_document, kb_id, owner, document_id, name, extension, count, digest))
        registration.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        job_id = await asyncio.shield(registration)
    except asyncio.CancelledError:
        # 注册线程无法由请求取消；保留原文件供已提交记录恢复。
        if registration is None:
            shutil.rmtree(directory)
        raise
    except Exception:
        if registration is None or not await asyncio.to_thread(document_registered, document_id):
            shutil.rmtree(directory)
        raise
    finally:
        await file.close()
    return await asyncio.to_thread(enqueue_saved, document_id, job_id)


def check_active(kb_id, owner):
    with session() as db:
        owned_kb(db, kb_id, owner, True)


def document_registered(document_id):
    with session() as db:
        return db.get(Document, document_id) is not None


@router.get('/knowledge-bases/{kb_id}/documents', response_model=list[DocumentOut])
def list_documents(kb_id: str, owner: str = Depends(current_user)):
    with session() as db:
        kb = owned_kb(db, kb_id, owner, False)
        return [DocumentOut.model_validate(doc) for doc in db.scalars(select(Document).where(Document.kb_id == kb.id).order_by(Document.created_at))]


@router.get('/documents/{document_id}', response_model=DocumentOut)
def document_status(document_id: str, owner: str = Depends(current_user)):
    with session() as db:
        return DocumentOut.model_validate(owned_document(db, document_id, owner, False))


@router.get('/documents/{document_id}/download')
def download(document_id: str, owner: str = Depends(current_user)):
    with session() as db:
        doc = owned_document(db, document_id, owner, False)
        if doc.parse_status == 'deleting':
            fail(409, 'document_deleting', '文档正在清理')
        path = stored_file(doc.original_path, doc.id)
        if not path.is_file():
            fail(404, 'file_missing', '原文件不存在')
        return FileResponse(path, filename=doc.original_name, media_type=doc.mime_type)


def schedule_again(document_id, owner, expected):
    from app.ingest.locks import document_lock
    from app.ingest.recovery import job_for, live_execution
    from app.ingest.writers import writer_alive
    with document_lock(document_id):
        if writer_alive(document_id):
            fail(409, 'writer_active', '旧执行进程尚未停止，请稍后重试', True)
        with session() as db:
            doc = owned_document(db, document_id, owner, True)
            kb = owned_kb(db, doc.kb_id, owner, True)
            db.refresh(kb, with_for_update=True)
            db.refresh(doc, with_for_update=True)
            if kb.status != 'active' or doc.parse_status != expected:
                fail(409, 'state_conflict', '文档状态不允许此操作')
            job = job_for(doc.job_id)
            if live_execution(job) or (job and job.get_status().value in {'queued', 'started', 'deferred', 'scheduled'}):
                fail(409, 'job_active', '旧任务尚未结束，请稍后重试', True)
            if expected == 'ready':
                if not doc.parsed_path or not stored_file(doc.parsed_path, doc.id).is_file():
                    fail(409, 'parsed_missing', '有效解析产物不存在')
                bump_version(db, kb.id)
            doc.parse_status, doc.stage, doc.job_id = 'queued', 'enqueue', str(uuid4())
            doc.error_code = doc.error_message = None
            db.commit()
            job_id = doc.job_id
        return enqueue_saved(document_id, job_id)


@router.post('/documents/{document_id}/retry', response_model=Queued, status_code=202, dependencies=[Depends(require_origin)])
def retry(document_id: str, owner: str = Depends(current_user)):
    return schedule_again(document_id, owner, 'failed')


@router.post('/documents/{document_id}/reindex', response_model=Queued, status_code=202, dependencies=[Depends(require_origin)])
def reindex(document_id: str, owner: str = Depends(current_user)):
    return schedule_again(document_id, owner, 'ready')

@router.delete('/documents/{document_id}', status_code=204, dependencies=[Depends(require_origin)])
def remove_document(document_id: str, owner: str = Depends(current_user)):
    from app.services.deletion import delete_document
    delete_document(document_id, owner)
