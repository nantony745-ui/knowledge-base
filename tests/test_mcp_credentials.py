from contextlib import nullcontext
import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.adapters.common import AdapterError
from app.agent.tools.mcp_tools import MCPTools
from app.api import mcp_services as api
from app.api.auth import current_user
from app.main import adapter_error, request_validation_error
from app.services import mcp_credentials as credentials
from app.settings import Settings
from dotenv import dotenv_values


@pytest.fixture
def online_client(tmp_path, monkeypatch):
    stale = Settings(_env_file=None, APP_ORIGIN='http://testserver')
    monkeypatch.setattr(Settings, 'MCP_CREDENTIALS_FILE', tmp_path / '.env.mcp')
    monkeypatch.setattr(credentials, 'load_settings', lambda: stale)
    monkeypatch.setattr(api, 'load_settings', lambda: stale)
    monkeypatch.setattr('app.api.auth.load_settings', lambda: stale)
    app = FastAPI()
    app.include_router(api.router)
    app.add_exception_handler(AdapterError, adapter_error)
    app.add_exception_handler(RequestValidationError, request_validation_error)
    def owner(request: Request):
        if request.cookies.get('test_admin') != 'yes':
            raise HTTPException(401)
        return 'owner'
    app.dependency_overrides[current_user] = owner
    with TestClient(app, headers={'Origin':'http://testserver'}) as client:
        client.cookies.set('test_admin', 'yes')
        yield client, stale


def test_online_credential_write_only_status_and_private_persistence(online_client):
    client, stale = online_client
    path = '/api/mcp-services/credentials/MCP_TAVILY_API_KEY'
    token = 'fixture-online-secret'
    response = client.put(path, json={'token': token})
    assert response.json() == {'auth_env_key':'MCP_TAVILY_API_KEY', 'configured':True}
    status = client.get('/api/mcp-services/credentials')
    assert token not in response.text + status.text
    assert any(row['auth_env_key'] == 'MCP_TAVILY_API_KEY' and row['configured'] for row in status.json())
    assert credentials.load_mcp_settings(stale).MCP_TAVILY_API_KEY.get_secret_value() == token
    assert stale.MCP_TAVILY_API_KEY is None
    assert Settings.MCP_CREDENTIALS_FILE.stat().st_mode & 0o777 == 0o600


def test_online_credentials_require_login_origin_and_registered_variable(online_client):
    client, _ = online_client
    path = '/api/mcp-services/credentials/MCP_TAVILY_API_KEY'
    client.cookies.clear()
    assert client.put(path, json={'token':'fixture-secret'}).status_code == 401
    client.cookies.set('test_admin', 'yes')
    assert client.put(path, headers={'Origin':'http://other.invalid'}, json={'token':'fixture-secret'}).status_code == 403
    response = client.put('/api/mcp-services/credentials/CHAT_API_KEY', json={'token':'fixture-secret'})
    assert response.status_code == 422 and 'fixture-secret' not in response.text
    assert not Settings.MCP_CREDENTIALS_FILE.exists()


@pytest.mark.parametrize('token', ['', 'Bearer fixture-secret', 'fixture-secret\r\nInjected:x'])
def test_invalid_bearer_input_not_echoed_or_saved(online_client, token):
    client, _ = online_client
    response = client.put('/api/mcp-services/credentials/MCP_TAVILY_API_KEY', json={'token':token})
    assert response.status_code == 422 and 'fixture-secret' not in response.text
    assert not Settings.MCP_CREDENTIALS_FILE.exists()


@pytest.mark.asyncio
async def test_existing_tool_handler_uses_online_credential_rotation(online_client, monkeypatch):
    client, stale = online_client
    seen = []
    async def call(adapter, snapshot, tool, arguments, recheck):
        seen.append(adapter.headers(snapshot['auth_env_key'])['Authorization'])
        return {'text':['ready']}
    monkeypatch.setattr('app.adapters.mcp.MCPAdapter.call', call)
    monkeypatch.setattr('app.agent.tools.mcp_tools.repository.recheck_service', lambda *args: None)
    observer = SimpleNamespace(tracer=SimpleNamespace(stage=lambda *args, **kwargs:
        nullcontext(SimpleNamespace(update=lambda **kwargs: None))))
    service = {'id':'service', 'auth_env_key':'MCP_TAVILY_API_KEY'}
    handler = MCPTools(stale, service, {'name':'tavily_search'}, observer)
    context = SimpleNamespace(user_id='owner', session_id='session')
    for token in ['fixture-first', 'fixture-second']:
        client.put('/api/mcp-services/credentials/MCP_TAVILY_API_KEY', json={'token':token}).raise_for_status()
        assert (await handler.invoke({'query':'MCP'}, context)).ok
    assert seen == ['Bearer fixture-first', 'Bearer fixture-second']


@pytest.fixture
def service_client(online_client, monkeypatch):
    client, stale = online_client
    records, headers = {}, []
    async def discover(adapter, endpoint, key, service_id):
        headers.append(adapter.headers(key))
        if endpoint.endswith('/bad'):
            raise AdapterError('mcp_http_error', False, 'MCP连接失败', None)
        return [{'name':'lookup', 'description':'只读', 'inputSchema':{'type':'object'}}]
    def save(owner, values, previous):
        identifier = previous['id'] if previous else values['id']
        stamp = datetime.now(timezone.utc)
        record = (previous or {'created_at':stamp}) | values | {'updated_at':stamp}
        records[identifier] = record
        return record
    monkeypatch.setattr('app.adapters.mcp.MCPAdapter.discover', discover)
    monkeypatch.setattr(api.repository, 'save_service', save)
    monkeypatch.setattr(api.repository, 'get_service', lambda identifier, owner: records[identifier].copy())
    monkeypatch.setattr(api.repository, 'list_services', lambda owner: list(records.values()))
    monkeypatch.setattr(api.repository, 'delete_service', lambda identifier, owner: records.pop(identifier))
    return client, stale, records, headers


def test_service_creation_keys_are_private_and_independent(service_client):
    client, stale, records, headers = service_client
    rows = []
    for key in ['fixture-key-one','fixture-key-two']:
        response = client.post('/api/mcp-services', json={'name':'独立服务','endpoint':'http://localhost/mcp',
            'requires_auth':True,'token':key})
        assert response.status_code == 201 and key not in response.text
        assert 'token' not in response.json()
        rows.append(response.json())
    assert rows[0]['auth_env_key'] != rows[1]['auth_env_key']
    adapter = api.MCPAdapter(credentials.load_mcp_settings(stale))
    assert adapter.headers(rows[0]['auth_env_key']) == {'Authorization':'Bearer fixture-key-one'}
    assert adapter.headers(rows[1]['auth_env_key']) == {'Authorization':'Bearer fixture-key-two'}
    response = client.post('/api/mcp-services', json={'name':'无认证','endpoint':'http://localhost/mcp'})
    assert response.status_code == 201 and response.json()['auth_env_key'] is None
    assert headers[-1] == {}
    assert all('token' not in record for record in records.values())


def test_service_key_rotation_failure_preserves_original_and_delete_cleans_secret(service_client):
    client, stale, records, headers = service_client
    row = client.post('/api/mcp-services', json={'name':'服务','endpoint':'http://localhost/mcp',
        'requires_auth':True,'token':'fixture-first', 'enabled_tools':['lookup']}).json()
    path = '/api/mcp-services/' + row['id']
    response = client.patch(path, json={'requires_auth':True,'token':'fixture-second'})
    assert response.status_code == 200 and response.json()['enabled_tools'] == ['lookup']
    changed = response.json()
    values = dotenv_values(Settings.MCP_CREDENTIALS_FILE)
    assert row['auth_env_key'] not in values and values[changed['auth_env_key']] == 'fixture-second'
    response = client.patch(path, json={'endpoint':'http://localhost/bad','requires_auth':True,'token':'fixture-failed'})
    assert response.status_code == 503 and records[row['id']]['endpoint'] == 'http://localhost/mcp'
    assert dotenv_values(Settings.MCP_CREDENTIALS_FILE) == values
    client.patch(path, json={'requires_auth':True,'name':'新名称'}).raise_for_status()
    assert headers[-1] == {'Authorization':'Bearer fixture-failed'}
    assert client.delete(path).status_code == 204
    assert not dotenv_values(Settings.MCP_CREDENTIALS_FILE)


def test_failed_create_cleans_key_and_rejects_missing_key_or_copied_reference(service_client):
    client, _, records, _ = service_client
    response = client.post('/api/mcp-services', json={'name':'失败','endpoint':'http://localhost/bad',
        'requires_auth':True,'token':'fixture-failed'})
    assert response.status_code == 503 and not records
    assert not dotenv_values(Settings.MCP_CREDENTIALS_FILE)
    assert client.post('/api/mcp-services', json={'name':'无Key','endpoint':'http://localhost/mcp','requires_auth':True}).status_code == 422
    assert client.post('/api/mcp-services', json={'name':'引用','endpoint':'http://localhost/mcp',
        'auth_env_key':'MCP_SERVICE_' + 'a' * 32}).status_code == 422


@pytest.mark.asyncio
async def test_cancel_after_database_commit_keeps_service_key(service_client, monkeypatch):
    _, stale, records, _ = service_client
    original = api.atomic_thread
    async def cancel_after_commit(function, *args, committed=None):
        result = await original(function, *args, committed=committed)
        if function is api.repository.save_service:
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(api, 'atomic_thread', cancel_after_commit)
    body = api.MCPCreate(name='提交后取消', endpoint='http://localhost/mcp', requires_auth=True, token='fixture-committed')
    with pytest.raises(asyncio.CancelledError):
        await api.create_service(body, 'owner')
    assert len(records) == 1
    record = next(iter(records.values()))
    assert api.MCPAdapter(credentials.load_mcp_settings(stale)).headers(record['auth_env_key']) == {'Authorization':'Bearer fixture-committed'}
