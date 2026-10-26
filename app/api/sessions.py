import asyncio
from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.api.auth import current_user, require_origin
from app.db.models import ConversationSession
from app.db.session import session
from app.services.conversations import check_session, delete_history, list_messages

router = APIRouter(prefix='/api/sessions', tags=['会话'])


class SessionInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    title: str = Field(min_length=1, max_length=255)


@router.post('', status_code=201, dependencies=[Depends(require_origin)])
def create_session(body: SessionInput, owner: str = Depends(current_user)):
    with session() as db:
        row = ConversationSession(id=str(uuid4()),owner_id=owner,title=body.title)
        db.add(row);db.commit()
        return {'id':row.id,'title':row.title}


@router.get('')
def sessions(owner: str = Depends(current_user)):
    with session() as db:
        return [{'id':s.id,'title':s.title,'updated_at':s.updated_at.isoformat()+'Z'}
                for s in db.scalars(select(ConversationSession).where(ConversationSession.owner_id==owner)
                                   .order_by(ConversationSession.updated_at.desc(),ConversationSession.id))]


@router.get('/{session_id}/messages')
def messages(session_id: str, owner: str = Depends(current_user)):
    return list_messages(session_id, owner)


@router.delete('/{session_id}', status_code=204, dependencies=[Depends(require_origin)])
async def remove_session(session_id: str, request: Request, owner: str = Depends(current_user)):
    await asyncio.to_thread(check_session, session_id, owner)
    manager = getattr(request.app.state, 'runs', None)
    if manager:
        await manager.delete_session(session_id, owner)
    else:
        await asyncio.to_thread(delete_history, session_id, owner)
