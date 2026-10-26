import asyncio
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.auth import current_user, require_origin
from app.api.common import fail
from app.db.source_repository import get_source
from app.agent.sources import SourceView

router = APIRouter(prefix='/api',tags=['问答运行'])


class RunInput(BaseModel):
    model_config = ConfigDict(extra='forbid',str_strip_whitespace=True)
    query: str = Field(min_length=1,max_length=4000)
    mode: Literal['quick_answer', 'smart_reasoning']
    kb_ids: list[str] = Field(default_factory=list,max_length=20)
    agent_id: None = None


@router.post('/sessions/{session_id}/runs',status_code=202,dependencies=[Depends(require_origin)])
async def create_run(session_id: str,body: RunInput,request: Request,owner: str=Depends(current_user)):
    run_id = await request.app.state.runs.create(session_id,owner,body.query,body.kb_ids,body.mode)
    return {'run_id':run_id}


@router.post('/runs/{run_id}/stop', status_code=204, dependencies=[Depends(require_origin)])
async def stop_run(run_id: str, request: Request, owner: str = Depends(current_user)):
    await request.app.state.runs.stop(run_id, owner)


async def event_stream(run,last_seq):
    while True:
        for event in run.events:
            if event['seq'] > last_seq:
                last_seq=event['seq']
                yield f'id: {last_seq}\ndata: {json.dumps(event,ensure_ascii=False)}\n\n'
        if run.terminal:
            return
        run.changed.clear()
        try:
            await asyncio.wait_for(run.changed.wait(),15)
        except TimeoutError:
            yield ': heartbeat\n\n'


@router.get('/runs/{run_id}/events')
async def events(run_id: str,request: Request,last_event_id: str | None=Header(None),owner: str=Depends(current_user)):
    run = request.app.state.runs.owned_run(run_id,owner)
    try:
        last = int(last_event_id or 0)
        if last < 0:raise ValueError()
    except ValueError:
        fail(422,'invalid_event_seq','Last-Event-ID必须是非负整数')
    return StreamingResponse(event_stream(run,last),media_type='text/event-stream',
        headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.get('/runs/{run_id}/sources/{handle}', response_model=SourceView)
def source(run_id: str, handle: str, owner: str = Depends(current_user)):
    from app.agent.sources import HANDLE
    if not HANDLE.fullmatch(handle):
        fail(404, 'source_not_found', '来源不存在')
    return get_source(run_id, owner, handle)
