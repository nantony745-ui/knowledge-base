import asyncio
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError
from jsonschema import ValidationError as SchemaValidationError

from app.adapters.common import AdapterError
from app.agent.tools.base import RemoteToolDefinition, ToolContext, ToolDefinition, ToolExecution, ToolResult, limit_model_result
from app.agent.tools.truncate import ModelResultTooLarge
from app.adapters.mcp import validate_arguments
from app.services.runs import atomic_thread
from app.settings import Settings


def _invalid_constant(value: str) -> None:
    raise ValueError(f'非法JSON常量：{value}')


class ToolRegistry:
    def __init__(self, settings: Settings, project_result: Callable[[str, ToolResult], ToolResult]):
        self.data_dir = settings.DATA_DIR
        self.max_chars = settings.TOOL_RESULT_MAX_CHARS
        self.project_result = project_result
        self.definitions: dict[str, ToolDefinition[Any] | RemoteToolDefinition] = {}

    def register(self, definition: ToolDefinition[Any] | RemoteToolDefinition) -> None:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', definition.name):
            raise ValueError('工具名无效')
        if definition.name in self.definitions:
            raise ValueError(f'工具重复注册：{definition.name}')
        self.definitions[definition.name] = definition

    def schemas(self, context: ToolContext) -> list[dict[str, Any]]:
        schemas = [{'type': 'function', 'function': {
            'name': tool.name, 'description': tool.description,
            'parameters': tool.input_schema if isinstance(tool, RemoteToolDefinition) else tool.args_model.model_json_schema(),
        }} for tool in self.definitions.values()
            if tool.read_only and tool.name in context.allowed_tools]
        return schemas

    async def execute(self, name: str, arguments: str, tool_call_id: str, context: ToolContext) -> ToolExecution:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', tool_call_id):
            result = ToolResult.failure('invalid_tool_call_id', '工具调用标识无效', False)
            return ToolExecution(tool_call_id, result, result.model_copy(deep=True))
        result = await self._dispatch(name, arguments, context)
        reference = f'runs/{context.run_id}/tools/{tool_call_id}.json'
        raw_result = result.model_copy(update={'raw_result_ref': reference}, deep=True)
        try:
            await atomic_thread(self._persist, name, tool_call_id, raw_result)
        except Exception:
            failure = ToolResult.failure('raw_result_write_failed', '完整工具结果未能保存，请重试', True)
            return ToolExecution(tool_call_id, result, failure)
        try:
            projected = self.project_result(name, raw_result)
            if name in {'list_documents', 'search_knowledge', 'read_document', 'read_chunk'}:
                projected = ToolResult.model_validate(context.refs.project(projected.model_dump(mode='json')))
            model_result = limit_model_result(projected, self.max_chars)
        except ModelResultTooLarge:
            parameter = {'list_documents': 'page_size', 'read_document': 'limit',
                         'read_chunk': 'before/after'}.get(name, '返回范围')
            model_result = ToolResult.failure('tool_result_too_large',
                f'工具结果的导航字段超过长度上限，请减少{parameter}后重试；完整结果已保存', False)
            model_result.raw_result_ref = reference
        except Exception:
            model_result = ToolResult.failure('tool_execution_failed', '工具结果处理失败', False)
            model_result.raw_result_ref = reference
        if context.cancel_event.is_set():
            model_result = ToolResult.failure('cancelled', '工具调用已取消', False)
            model_result.raw_result_ref = reference
        return ToolExecution(tool_call_id, raw_result, model_result)

    async def _dispatch(self, name: str, arguments: str, context: ToolContext) -> ToolResult:
        tool = self.definitions.get(name)
        if tool is None:
            return ToolResult.failure('unknown_tool', '工具不存在', False)
        if name not in context.allowed_tools or not tool.read_only:
            return ToolResult.failure('tool_disabled', '本次运行未启用该工具', False)
        if context.cancel_event.is_set():
            return ToolResult.failure('cancelled', '工具调用已取消', False)
        try:
            parsed = json.loads(arguments, parse_constant=_invalid_constant)
        except (ValueError, TypeError):
            return ToolResult.failure('invalid_json', '工具参数不是有效JSON', False)
        try:
            args = validate_arguments(tool.input_schema, parsed) if isinstance(tool, RemoteToolDefinition) else tool.args_model.model_validate(parsed)
        except (ValidationError, SchemaValidationError):
            return ToolResult.failure('invalid_arguments', '工具参数不符合schema', False)
        try:
            return await self._invoke(tool, args, context)
        except HTTPException as error:
            detail = error.detail
            if isinstance(detail, dict):
                return ToolResult.failure(detail['code'], detail['message'], detail.get('retryable', False))
            return ToolResult.failure('tool_execution_failed', '工具调用失败', error.status_code >= 500)
        except AdapterError as error:
            return ToolResult.failure(error.info.code, error.info.message, error.info.retryable)
        except Exception:
            return ToolResult.failure('tool_execution_failed', '工具调用失败', False)

    async def _invoke(self, tool: ToolDefinition[Any], args: Any, context: ToolContext) -> ToolResult:
        handler = asyncio.create_task(tool.handler(args, context))
        cancelled = asyncio.create_task(context.cancel_event.wait())
        try:
            await asyncio.wait((handler, cancelled), return_when=asyncio.FIRST_COMPLETED)
            if context.cancel_event.is_set():
                return ToolResult.failure('cancelled', '工具调用已取消', False)
            return await handler
        finally:
            for task in (handler, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(handler, cancelled, return_exceptions=True)

    def _persist(self, name: str, tool_call_id: str, result: ToolResult) -> None:
        root = self.data_dir.resolve()
        path = (root / Path(result.raw_result_ref)).resolve()
        if not path.is_relative_to(root):
            raise ValueError('工具结果路径越界')
        record = json.dumps({'name': name, 'tool_call_id': tool_call_id,
                             'result': result.model_dump(mode='json')}, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as output:
            try:
                output.write(record)
            except OSError:
                path.unlink()
                raise
