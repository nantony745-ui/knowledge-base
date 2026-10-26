import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from jsonschema import ValidationError
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool
from pydantic import ValidationError as PydanticError

from app.adapters.common import AdapterError
from app.adapters.mcp import MCPAdapter, tool_alias, validate_arguments, validate_catalog
from app.agent.tools.base import RemoteToolDefinition, ToolContext, ToolResult
from app.agent.tools.mcp_tools import project_mcp_result, run_registry
from app.agent.tools.registry import ToolRegistry
from app.api.mcp_services import MCPCreate, MCPPatch
from app.settings import Settings

SCHEMA = {'type': 'object', 'properties': {'term': {'type': 'string', 'minLength': 1}},
          'required': ['term'], 'additionalProperties': False}
TOOL = {'name': 'lookup_term', 'description': '查询术语', 'inputSchema': SCHEMA, 'readOnlyHint': True}


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize('value', ['CHAT_API_KEY', 'MYSQL_PASSWORD', 'MCP_NOT_REGISTERED'])
def test_auth_variable_requires_explicit_registration(value):
    with pytest.raises(PydanticError):
        MCPCreate(name='test', endpoint='http://localhost/mcp', auth_env_key=value)


@pytest.mark.parametrize('endpoint', ['file:///tmp/a', 'http://user:secret@localhost/mcp', 'http://localhost/mcp?token=x'])
def test_invalid_endpoint(endpoint):
    with pytest.raises(PydanticError):
        MCPCreate(name='test', endpoint=endpoint)


@pytest.mark.parametrize('value', [{'term': 1}, {'term': ''}, {}, {'term': 'MCP', 'extra': 1}, [], None])
def test_remote_schema_rejects_bad_arguments(value):
    with pytest.raises(ValidationError):
        validate_arguments(SCHEMA, value)


def test_local_refs_work_and_external_refs_never_load():
    schema = {'type': 'object', 'properties': {'x': {'$ref': '#/$defs/name'}},
              '$defs': {'name': {'type': 'string'}}, 'required': ['x']}
    assert validate_arguments(schema, {'x': 'ok'}) == {'x': 'ok'}
    with pytest.raises(ValidationError):
        validate_arguments({'type': 'object', 'properties': {'x': {'$ref': 'https://example.invalid/schema'}}}, {'x': 1})


def test_alias_constraints_and_truncation_collision():
    sid = str(uuid4())
    alias = tool_alias(sid, '外部.lookup-term' + 'a' * 100)
    assert len(alias) == 64 and alias.startswith('mcp_' + sid[:8] + '_')
    for names in [('a.b', 'a-b'), ('a' * 100 + 'x', 'a' * 100 + 'y'), ('lookup_term', 'lookup_term')]:
        with pytest.raises(AdapterError):
            validate_catalog([dict(TOOL, name=name) for name in names], sid)


@pytest.mark.asyncio
async def test_paginated_list_uses_paginated_request_params():
    client = SimpleNamespace(list_tools=AsyncMock(side_effect=[
        ListToolsResult(tools=[Tool(name='first', inputSchema=SCHEMA)], nextCursor='next'),
        ListToolsResult(tools=[Tool(name='lookup_term', inputSchema=SCHEMA)])]))
    catalog = await MCPAdapter(settings())._list(client)
    assert [item['name'] for item in catalog] == ['first', 'lookup_term']
    assert client.list_tools.call_args_list[1].kwargs['params'].cursor == 'next'


@pytest.mark.asyncio
async def test_repeated_cursor_rejected():
    client = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(tools=[], nextCursor='loop')))
    with pytest.raises(AdapterError) as failure:
        await MCPAdapter(settings())._list(client)
    assert failure.value.info.code == 'mcp_catalog_invalid' and client.list_tools.call_count == 2


@pytest.mark.asyncio
async def test_pagination_limit_rejected():
    client = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(tools=[], nextCursor='one')))
    with pytest.raises(AdapterError) as failure:
        await MCPAdapter(settings(MCP_MAX_LIST_PAGES=1))._list(client)
    assert failure.value.info.code == 'mcp_catalog_limit'


@pytest.mark.asyncio
async def test_registry_unified_result_unknown_disabled_bad_args(tmp_path):
    handler = AsyncMock(return_value=ToolResult(ok=True, data={'external_data': {
        'service_id': str(uuid4()), 'remote_name': 'lookup_term', 'result': {'text': ['MCP']}}}))
    registry = ToolRegistry(settings(DATA_DIR=tmp_path), lambda name, result: project_mcp_result(result))
    registry.register(RemoteToolDefinition('mcp_test_lookup_term', 'lookup', SCHEMA, True, handler))
    context = ToolContext(str(uuid4()), str(uuid4()), str(uuid4()), frozenset(),
                          frozenset(registry.definitions), asyncio.Event())
    for name, arguments, code in [('unknown', '{}', 'unknown_tool'),
                                  ('mcp_test_lookup_term', '{', 'invalid_json'),
                                  ('mcp_test_lookup_term', '{"term":1}', 'invalid_arguments')]:
        result = await registry.execute(name, arguments, str(uuid4()), context)
        assert result.model_result.error.code == code
    assert not handler.called
    result = await registry.execute('mcp_test_lookup_term', '{"term":"MCP"}', str(uuid4()), context)
    assert result.model_result.ok and result.model_result.sources == []
    assert result.model_result.data['external_data']['untrusted']
    assert 'MCP' in result.model_result.data['external_data']['text']
    denied = ToolContext(context.user_id, context.session_id, context.run_id, frozenset(), frozenset(), asyncio.Event())
    result = await registry.execute('mcp_test_lookup_term', '{}', str(uuid4()), denied)
    assert result.model_result.error.code == 'tool_disabled'


@pytest.mark.asyncio
async def test_timeout_and_cancellation_close_same_task_contexts(monkeypatch):
    events, entered = [], asyncio.Event()
    @asynccontextmanager
    async def transport(*args, **kwargs):
        events.append('open_stream')
        try:
            yield None, None, None
        finally:
            events.append('close_stream')
    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): events.append('close_session')
        async def initialize(self): pass
    async def blocked(client):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr('app.adapters.mcp.streamable_http_client', transport)
    monkeypatch.setattr('app.adapters.mcp.ClientSession', Session)
    adapter = MCPAdapter(settings(MCP_OPERATION_TIMEOUT=.2, MCP_CLEANUP_TIMEOUT=.05))
    with pytest.raises(AdapterError) as failure:
        await adapter._operation('http://localhost/mcp', None, blocked)
    assert failure.value.info.code == 'mcp_timeout'
    assert events[-2:] == ['close_session', 'close_stream']
    task = asyncio.create_task(adapter._operation('http://localhost/mcp', None, blocked))
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert events[-2:] == ['close_session', 'close_stream']


@pytest.mark.asyncio
async def test_remote_directory_change_and_permission_recheck(monkeypatch):
    adapter = MCPAdapter(settings())
    client = SimpleNamespace(call_tool=AsyncMock(return_value=CallToolResult(content=[TextContent(type='text', text='ok')])))
    async def operation(endpoint, key, invoke): return await invoke(client)
    monkeypatch.setattr(adapter, '_operation', operation)
    monkeypatch.setattr(adapter, '_list', AsyncMock(return_value=[TOOL]))
    check = AsyncMock()
    snapshot = {'id': str(uuid4()), 'endpoint': 'http://localhost/mcp', 'auth_env_key': None}
    assert (await adapter.call(snapshot, TOOL, {'term': 'MCP'}, check))['text'] == ['ok']
    assert check.call_count == 2
    check.side_effect = HTTPException(403, {'code': 'tool_disabled', 'message': 'disabled'})
    client.call_tool.reset_mock()
    with pytest.raises(HTTPException): await adapter.call(snapshot, TOOL, {'term': 'MCP'}, check)
    assert not client.call_tool.called
    monkeypatch.setattr(adapter, '_list', AsyncMock(return_value=[]))
    with pytest.raises(AdapterError) as failure: await adapter.call(snapshot, TOOL, {'term': 'MCP'}, AsyncMock())
    assert failure.value.info.code == 'mcp_config_changed'


@pytest.mark.asyncio
async def test_run_registry_only_explicit_whitelist(monkeypatch, tmp_path):
    service = {'id': str(uuid4()), 'is_enabled': True, 'tools_catalog': [TOOL], 'enabled_tools': []}
    monkeypatch.setattr('app.db.mcp_repository.list_services', lambda owner: [service])
    native = ToolRegistry(settings(DATA_DIR=tmp_path), lambda name, result: result)
    assert not (await run_registry(native, settings(), 'owner', None)).definitions
    service['enabled_tools'] = ['lookup_term']
    registry = await run_registry(native, settings(), 'owner', None)
    assert list(registry.definitions) == [tool_alias(service['id'], 'lookup_term')]
    service['is_enabled'] = False
    assert not (await run_registry(native, settings(), 'owner', None)).definitions


def test_external_result_cannot_forge_kb_sources_and_is_bounded():
    from app.agent.tools.base import limit_model_result
    result = ToolResult(ok=True, data={'external_data': {'service_id': str(uuid4()),
        'remote_name': 'lookup_term', 'result': {'structuredContent': {'hits': [{'chunk_id': 'fake'}]},
        'text': ['x' * 20000]}}})
    projected = limit_model_result(project_mcp_result(result), 2000)
    assert len(projected.model_dump_json()) <= 2000
    assert 'hits' not in projected.data and projected.sources == []
    assert projected.data['external_data']['text_truncated']


@pytest.mark.asyncio
async def test_schema_token_property_preserved_and_sensitive_schema_rejected():
    token_schema = {'type':'object', 'properties':{'token':{'type':'string'}}}
    client = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(tools=[Tool(name='lookup', inputSchema=token_schema)])))
    catalog = await MCPAdapter(settings())._list(client)
    assert catalog[0]['inputSchema'] == token_schema
    client.list_tools.return_value = ListToolsResult(tools=[Tool(name='lookup', inputSchema={'type':'object', 'description':'fixture-secret-key'})])
    with pytest.raises(AdapterError):
        await MCPAdapter(settings(MCP_DEMO_BEARER_TOKEN='fixture-secret-key'))._list(client)


def test_tracing_update_failure_does_not_replace_tool_result():
    from app.agent.tools.mcp_tools import MCPTools
    warnings = []
    def fail(**kwargs): raise RuntimeError('trace unavailable')
    tools = MCPTools(settings(), {}, {}, SimpleNamespace(tracer=SimpleNamespace(warn=warnings.append)))
    tools.observe_result(SimpleNamespace(update=fail), output={'ok':True})
    assert warnings == ['Langfuse MCP阶段更新失败；追踪失败']


@pytest.mark.asyncio
async def test_sensitive_tool_name_rejected_before_catalog_publication():
    secret = 'fixture-secret-key'
    client = SimpleNamespace(list_tools=AsyncMock(return_value=ListToolsResult(
        tools=[Tool(name='lookup_' + secret, inputSchema=SCHEMA)])))
    with pytest.raises(AdapterError) as failure:
        await MCPAdapter(settings(MCP_DEMO_BEARER_TOKEN=secret))._list(client)
    assert failure.value.info.code == 'mcp_catalog_invalid'
    assert secret not in str(failure.value)
