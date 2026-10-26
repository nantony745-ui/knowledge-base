from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.auth import current_user, require_origin
from app.api.common import owned_kb
from app.api.schemas import KBCreate, KBOut, KBUpdate
from app.db.models import KnowledgeBase
from app.db.session import session

router = APIRouter(prefix='/api/knowledge-bases', tags=['知识库'])


@router.post('', response_model=KBOut, status_code=201, dependencies=[Depends(require_origin)])
def create_kb(body: KBCreate, owner: str = Depends(current_user)):
    with session() as db:
        kb = KnowledgeBase(id=str(uuid4()), owner_id=owner, **body.model_dump())
        db.add(kb)
        db.commit()
        db.refresh(kb)
        return KBOut.model_validate(kb)


@router.get('', response_model=list[KBOut])
def list_kb(owner: str = Depends(current_user)):
    with session() as db:
        return [KBOut.model_validate(kb) for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.owner_id == owner).order_by(KnowledgeBase.created_at))]


@router.patch('/{kb_id}', response_model=KBOut, dependencies=[Depends(require_origin)])
def edit_kb(kb_id: str, body: KBUpdate, owner: str = Depends(current_user)):
    with session() as db:
        kb = owned_kb(db, kb_id, owner, True)
        db.refresh(kb, with_for_update=True)
        owned_kb(db, kb_id, owner, True)
        for key, value in body.model_dump(exclude_unset=True).items():
            setattr(kb, key, value)
        db.commit()
        db.refresh(kb)
        return KBOut.model_validate(kb)


@router.delete('/{kb_id}', status_code=204, dependencies=[Depends(require_origin)])
def remove_kb(kb_id: str, owner: str = Depends(current_user)):
    from app.services.deletion import delete_kb
    delete_kb(kb_id, owner)
