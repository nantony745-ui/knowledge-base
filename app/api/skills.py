from fastapi import APIRouter, Depends, Request

from pydantic import BaseModel, ConfigDict

from app.adapters.common import AdapterError
from app.api.auth import current_user, require_origin
from app.api.common import fail
from app.sandbox.skills import SkillStatus
from app.services.runs import atomic_thread


router = APIRouter(prefix='/api/skills', tags=['本地技能'])


class SkillUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    is_enabled: bool


@router.get('', response_model=list[SkillStatus], dependencies=[Depends(current_user)])
async def list_skills(request: Request):
    return request.app.state.skills.statuses()


@router.post('/refresh', response_model=list[SkillStatus], dependencies=[Depends(current_user), Depends(require_origin)])
async def refresh_skills(request: Request):
    return await atomic_thread(request.app.state.skills.refresh)


@router.patch('/{name}', response_model=SkillStatus, dependencies=[Depends(current_user), Depends(require_origin)])
async def update_skill(name: str, body: SkillUpdate, request: Request):
    catalog = request.app.state.skills
    try:
        return await atomic_thread(catalog.set_enabled, name, body.is_enabled)
    except AdapterError as error:
        if error.info.code == 'unknown_skill':
            fail(404, 'unknown_skill', '技能未登记')
        raise
