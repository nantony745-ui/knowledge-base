from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.adapters.mcp import MCPAdapter, validate_endpoint
from app.api.auth import current_user, require_origin
from app.api.common import fail
from app.db import mcp_repository as repository
from app.services.runs import atomic_thread
from app.services.mcp_credentials import (credential_status, load_mcp_settings, save_mcp_credential,
                                         new_service_credential, remove_service_credential)
from app.settings import load_settings

router = APIRouter(prefix='/api/mcp-services', tags=['MCP服务'])


class MCPCredential(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    token: SecretStr = Field(min_length=1, max_length=4096)

    @field_validator('token')
    @classmethod
    def bearer_token(cls, value: SecretStr) -> SecretStr:
        if any(character.isspace() or ord(character) < 32 for character in value.get_secret_value()):
            raise ValueError('只填写不含空白的API Key或Bearer Token')
        return value


async def current_adapter() -> MCPAdapter:
    return MCPAdapter(await atomic_thread(load_mcp_settings, load_settings()))


@router.get('/credentials')
async def list_credentials(owner: str = Depends(current_user)):
    return credential_status((await current_adapter()).settings)


@router.put('/credentials/{auth_env_key}', dependencies=[Depends(require_origin)])
async def set_credential(auth_env_key: str, body: MCPCredential, owner: str = Depends(current_user)):
    if auth_env_key not in load_settings().MCP_AUTH_ENV_KEYS:
        fail(422, 'mcp_auth_unregistered', '认证变量未登记为MCP凭据')
    await atomic_thread(save_mcp_credential, auth_env_key, body.token)
    return {'auth_env_key': auth_env_key, 'configured': True}


class MCPCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2048)
    auth_env_key: str | None = Field(None, max_length=128)
    enabled_tools: list[str] = Field(default_factory=list)
    is_enabled: bool = True
    requires_auth: bool = False
    token: SecretStr | None = Field(None, min_length=1, max_length=4096, exclude=True)
    _endpoint = field_validator('endpoint')(validate_endpoint)
    _token = field_validator('token')(lambda value: MCPCredential.bearer_token(value) if value is not None else value)

    @field_validator('auth_env_key')
    @classmethod
    def registered_auth(cls, value: str | None) -> str | None:
        if value is not None and value not in load_settings().MCP_AUTH_ENV_KEYS:
            raise ValueError('认证变量未登记为MCP凭据')
        return value


class MCPPatch(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, hide_input_in_errors=True)
    name: str = Field(min_length=1, max_length=128, default=None)
    endpoint: str = Field(min_length=1, max_length=2048, default=None)
    auth_env_key: str | None = Field(None, max_length=128)
    enabled_tools: list[str] = Field(default=None)
    is_enabled: bool = Field(default=None)
    requires_auth: bool = Field(default=None)
    token: SecretStr | None = Field(None, min_length=1, max_length=4096, exclude=True)
    _endpoint = field_validator('endpoint')(validate_endpoint)
    _auth = field_validator('auth_env_key')(MCPCreate.registered_auth)
    _token = field_validator('token')(lambda value: MCPCredential.bearer_token(value) if value is not None else value)


class MCPOut(BaseModel):
    model_config = ConfigDict(extra='ignore')
    name: str
    endpoint: str
    auth_env_key: str | None
    enabled_tools: list[str]
    is_enabled: bool
    id: str
    tools_catalog: list[dict]
    created_at: datetime
    updated_at: datetime


@router.post('', response_model=MCPOut, status_code=201, dependencies=[Depends(require_origin)])
async def create_service(body: MCPCreate, owner: str = Depends(current_user)):
    values = body.model_dump(exclude={'requires_auth'})
    values['enabled_tools'] = list(dict.fromkeys(values['enabled_tools']))
    values['id'] = str(uuid4())
    reference = None
    if body.requires_auth:
        if body.token is None:
            fail(422, 'mcp_auth_missing', '需要认证时请填写Key')
        reference = new_service_credential()
        values['auth_env_key'] = reference
    elif body.token is not None:
        fail(422, 'mcp_auth_invalid', '填写Key时请选择需要认证')
    committed = []
    try:
        if reference:
            await atomic_thread(save_mcp_credential, reference, body.token)
        values['tools_catalog'] = await (await current_adapter()).discover(body.endpoint, values['auth_env_key'], values['id'])
        return await atomic_thread(repository.save_service, owner, values, None, committed=committed.append)
    except BaseException:
        if not committed:
            await atomic_thread(remove_service_credential, reference)
        raise


@router.get('', response_model=list[MCPOut])
async def list_services(owner: str = Depends(current_user)):
    return await atomic_thread(repository.list_services, owner)


@router.patch('/{service_id}', response_model=MCPOut, dependencies=[Depends(require_origin)])
async def edit_service(service_id: str, body: MCPPatch, owner: str = Depends(current_user)):
    previous = await atomic_thread(repository.get_service, service_id, owner)
    values = body.model_dump(exclude_unset=True, exclude={'requires_auth'})
    reference = None
    if body.requires_auth is False:
        if body.token is not None:
            fail(422, 'mcp_auth_invalid', '无需认证时不能提交Key')
        values['auth_env_key'] = None
    elif body.token is not None:
        reference = new_service_credential()
        values['auth_env_key'] = reference
    elif body.requires_auth and not previous['auth_env_key']:
        fail(422, 'mcp_auth_missing', '需要认证时请填写Key')
    committed = []
    try:
        if reference:
            await atomic_thread(save_mcp_credential, reference, body.token)
        if any(key in values and values[key] != previous[key] for key in ('endpoint', 'auth_env_key')):
            updated = previous | values
            values['tools_catalog'] = await (await current_adapter()).discover(updated['endpoint'], updated['auth_env_key'], service_id)
            old = {tool['name']: tool['inputSchema'] for tool in previous['tools_catalog']}
            unchanged = {tool['name'] for tool in values['tools_catalog'] if old.get(tool['name']) == tool['inputSchema']}
            values.setdefault('enabled_tools', [name for name in previous['enabled_tools'] if name in unchanged]
                              if reference and updated['endpoint'] == previous['endpoint'] else [])
        if 'enabled_tools' in values:
            values['enabled_tools'] = list(dict.fromkeys(values['enabled_tools']))
        saved = await atomic_thread(repository.save_service, owner, values, previous, committed=committed.append)
    except BaseException:
        if not committed:
            await atomic_thread(remove_service_credential, reference)
        raise
    finally:
        if committed and committed[0]['auth_env_key'] != previous['auth_env_key']:
            await atomic_thread(remove_service_credential, previous['auth_env_key'])
    return saved


@router.delete('/{service_id}', status_code=204, dependencies=[Depends(require_origin)])
async def remove_service(service_id: str, owner: str = Depends(current_user)):
    previous = await atomic_thread(repository.get_service, service_id, owner)
    committed = []
    try:
        await atomic_thread(repository.delete_service, service_id, owner, committed=committed.append)
    finally:
        if committed:
            await atomic_thread(remove_service_credential, previous['auth_env_key'])


@router.post('/{service_id}/test', dependencies=[Depends(require_origin)])
async def test_service(service_id: str, owner: str = Depends(current_user)):
    previous = await atomic_thread(repository.get_service, service_id, owner)
    catalog = await (await current_adapter()).discover(previous['endpoint'], previous['auth_env_key'], service_id)
    return {'connected': True, 'tool_count': len(catalog)}


@router.post('/{service_id}/refresh-tools', response_model=MCPOut, dependencies=[Depends(require_origin)])
async def refresh_tools(service_id: str, owner: str = Depends(current_user)):
    previous = await atomic_thread(repository.get_service, service_id, owner)
    catalog = await (await current_adapter()).discover(previous['endpoint'], previous['auth_env_key'], service_id)
    old = {tool['name']: tool['inputSchema'] for tool in previous['tools_catalog']}
    unchanged = {tool['name'] for tool in catalog if old.get(tool['name']) == tool['inputSchema']}
    return await atomic_thread(repository.save_service, owner, {'tools_catalog': catalog,
        'enabled_tools': [name for name in previous['enabled_tools'] if name in unchanged]}, previous)
