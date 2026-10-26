import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from jsonschema.validators import validator_for
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams
from referencing import Registry
from referencing.exceptions import Unresolvable

from app.adapters.common import AdapterError, Sanitizer
from app.settings import Settings
from app.services.mcp_credentials import is_service_credential

AUTH_ENV_KEYS = Settings.MCP_AUTH_ENV_KEYS


def mcp_error(code: str, message: str, retryable: bool = False) -> AdapterError:
    return AdapterError(code, retryable, message, None)


def validate_endpoint(value: str) -> str:
    url = urlsplit(value)
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('需要不含凭据、查询参数或片段的HTTP(S)地址')
    return value


def validate_auth_key(value: str | None) -> str | None:
    if value is not None and value not in AUTH_ENV_KEYS and not is_service_credential(value):
        raise ValueError('认证变量未登记为MCP凭据')
    return value


def tool_alias(service_id: str, remote_name: str) -> str:
    return ('mcp_' + service_id[:8] + '_' + re.sub(r'[^A-Za-z0-9_]', '_', remote_name))[:64]


def schema_validator(schema: dict):
    validator = validator_for(schema, default=Draft202012Validator)
    if '$schema' in schema and validator_for(schema, default=None) is None:
        raise SchemaError('不支持的JSON Schema版本')
    validator.check_schema(schema)
    return validator(schema, registry=Registry())


def validate_arguments(schema: dict, arguments: Any) -> dict:
    if not isinstance(arguments, dict):
        raise ValidationError('MCP参数必须是JSON object')
    try:
        schema_validator(schema).validate(arguments)
    except Unresolvable:
        raise ValidationError('schema引用无法在本地解析') from None
    return arguments


def validate_catalog(catalog: list[dict], service_id: str) -> None:
    names, aliases = set(), set()
    for tool in catalog:
        name, alias = tool['name'], tool_alias(service_id, tool['name'])
        if not name or name in names or alias in aliases:
            raise mcp_error('mcp_catalog_conflict', '远端工具名重复或别名冲突')
        names.add(name)
        aliases.add(alias)
        try:
            schema_validator(tool['inputSchema'])
        except (SchemaError, TypeError, ValueError):
            raise mcp_error('mcp_invalid_schema', '远端工具schema无效') from None


class MCPAdapter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sanitizer = Sanitizer([value.get_secret_value() for value in settings.__dict__.values()
                                    if hasattr(value, 'get_secret_value')])

    def headers(self, auth_env_key: str | None) -> dict[str, str]:
        validate_auth_key(auth_env_key)
        if auth_env_key is None:
            return {}
        secret = getattr(self.settings, auth_env_key, None)
        if not secret or not secret.get_secret_value():
            raise mcp_error('mcp_auth_missing', 'MCP服务认证Key未配置，请编辑服务后保存Key')
        return {'Authorization': 'Bearer ' + secret.get_secret_value()}

    async def _operation(self, endpoint: str, auth_env_key: str | None,
                         operation: Callable[[ClientSession], Awaitable[Any]]) -> Any:
        validate_endpoint(endpoint)
        headers = self.headers(auth_env_key)
        total = self.settings.MCP_OPERATION_TIMEOUT
        cleanup = min(self.settings.MCP_CLEANUP_TIMEOUT, total / 2)
        try:
            async with asyncio.timeout(total):
                async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(cleanup)) as http:
                    async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
                        async with ClientSession(read, write) as client:
                            async with asyncio.timeout(total - cleanup):
                                await client.initialize()
                                return await operation(client)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures = self._leaves(error)
            own = next((item for item in failures if isinstance(item, AdapterError)), None)
            if own:
                raise own from None
            permission = next((item for item in failures if isinstance(item, HTTPException)), None)
            if permission:
                raise permission from None
            if any(isinstance(item, (TimeoutError, httpx.TimeoutException)) for item in failures):
                raise mcp_error('mcp_timeout', 'MCP操作超时', True) from None
            if any(isinstance(item, httpx.HTTPStatusError) for item in failures):
                raise mcp_error('mcp_http_error', 'MCP服务返回HTTP错误', True) from None
            if any(isinstance(item, (httpx.TransportError, EOFError)) for item in failures):
                raise mcp_error('mcp_disconnected', 'MCP连接断开', True) from None
            raise mcp_error('mcp_protocol_error', 'MCP协议或结果无效', False) from None

    @staticmethod
    def _leaves(error: BaseException) -> list[BaseException]:
        if isinstance(error, BaseExceptionGroup):
            return [leaf for child in error.exceptions for leaf in MCPAdapter._leaves(child)]
        return [error]

    async def _list(self, client: ClientSession) -> list[dict]:
        catalog, seen, cursor = [], set(), None
        for _ in range(self.settings.MCP_MAX_LIST_PAGES):
            page = await client.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
            for tool in page.tools:
                if len(json.dumps(tool.inputSchema).encode('utf-8')) > self.settings.MCP_MAX_SCHEMA_BYTES:
                    raise mcp_error('mcp_catalog_limit', '远端schema超过大小上限')
                serialized_schema = json.dumps(tool.inputSchema, ensure_ascii=False)
                if any(secret in serialized_schema for secret in self.sanitizer.secrets):
                    raise mcp_error('mcp_invalid_schema', '远端schema包含敏感配置')
                if any(secret in tool.name for secret in self.sanitizer.secrets):
                    raise mcp_error('mcp_catalog_invalid', '远端工具名包含敏感配置')
                catalog.append({'name': tool.name, 'description': self.sanitizer.text(tool.description or ''),
                                'inputSchema': tool.inputSchema,
                                'readOnlyHint': tool.annotations.readOnlyHint if tool.annotations else None})
            if len(catalog) > self.settings.MCP_MAX_TOOLS:
                raise mcp_error('mcp_catalog_limit', '远端工具目录超过数量上限')
            cursor = page.nextCursor
            if not cursor:
                return catalog
            if cursor in seen:
                raise mcp_error('mcp_catalog_invalid', '远端分页cursor重复')
            seen.add(cursor)
        raise mcp_error('mcp_catalog_limit', '远端工具目录超过分页上限')

    async def discover(self, endpoint: str, auth_env_key: str | None, service_id: str) -> list[dict]:
        catalog = await self._operation(endpoint, auth_env_key, self._list)
        validate_catalog(catalog, service_id)
        return catalog

    async def call(self, snapshot: dict, tool: dict, arguments: dict,
                   recheck: Callable[[], Awaitable[None]]) -> dict:
        async def invoke(client):
            catalog = await self._list(client)
            validate_catalog(catalog, snapshot['id'])
            current = next((item for item in catalog if item['name'] == tool['name']), None)
            if current is None or current['inputSchema'] != tool['inputSchema']:
                raise mcp_error('mcp_config_changed', '远端目录已变化，请刷新目录后重新运行')
            validate_arguments(current['inputSchema'], arguments)
            await recheck()
            result = await client.call_tool(tool['name'], arguments)
            await recheck()
            if result.isError:
                raise mcp_error('mcp_remote_error', '远端工具返回失败')
            text = [item.text for item in result.content if item.type == 'text']
            unsupported = [item.type for item in result.content if item.type != 'text']
            return self.sanitizer.clean({'text': text, 'structuredContent': result.structuredContent,
                                        'unsupported_content_types': unsupported})
        return await self._operation(snapshot['endpoint'], snapshot['auth_env_key'], invoke)
