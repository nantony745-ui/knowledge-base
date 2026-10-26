from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update

from app.db.models import Document, KnowledgeBase


def fail(status: int, code: str, message: str, retryable: bool = False):
    raise HTTPException(status, detail={'code': code, 'message': message, 'retryable': retryable})


def identifier(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        fail(422, 'invalid_id', '资源标识不是有效UUID')


def owned_kb(db, kb_id: str, owner: str, active: bool):
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == identifier(kb_id), KnowledgeBase.owner_id == owner))
    if kb is None:
        fail(404, 'not_found', '知识库不存在')
    if active and kb.status != 'active':
        fail(409, 'kb_deleting', '知识库正在清理')
    return kb


def owned_document(db, document_id: str, owner: str, active: bool):
    doc = db.get(Document, identifier(document_id))
    if doc is None:
        fail(404, 'not_found', '文档不存在')
    owned_kb(db, doc.kb_id, owner, active)
    return doc


def bump_version(db, kb_id: str):
    db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb_id).values(index_version=KnowledgeBase.index_version + 1))
