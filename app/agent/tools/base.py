import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.retrieval import authorize
from app.agent.refs import RunRefs
from app.services.conversations import check_session
from app.services.runs import atomic_thread
from app.agent.tools.truncate import truncate_result
from app.settings import Settings

MODEL_RESULT_MAX_CHARS = Settings.model_fields['TOOL_RESULT_MAX_CHARS'].default
Args = TypeVar('Args', bound=BaseModel)


@dataclass(frozen=True)
class ToolContext:
    user_id: str
    session_id: str
    run_id: str
    kb_ids: frozenset[str]
    allowed_tools: frozenset[str]
    cancel_event: asyncio.Event
    refs: RunRefs = field(default_factory=RunRefs, compare=False)

    def __post_init__(self) -> None:
        for name in ('user_id', 'session_id', 'run_id'):
            object.__setattr__(self, name, str(UUID(getattr(self, name))))
        object.__setattr__(self, 'kb_ids', frozenset(str(UUID(k)) for k in self.kb_ids))
        object.__setattr__(self, 'allowed_tools', frozenset(self.allowed_tools))


async def create_tool_context(
    user_id: str, session_id: str, run_id: str, kb_ids: Iterable[str],
    allowed_tools: Iterable[str], cancel_event: asyncio.Event,
    refs: RunRefs | None = None,
) -> ToolContext:
    context = ToolContext(user_id, session_id, run_id, frozenset(kb_ids),
                          frozenset(allowed_tools), cancel_event, refs if refs is not None else RunRefs())
    await atomic_thread(check_session, context.session_id, context.user_id)
    await atomic_thread(authorize, context.user_id, sorted(context.kb_ids))
    return context


class ToolError(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str
    message: str
    retryable: bool


class ToolResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ok: bool
    data: Any = None
    sources: list[dict[str, Any]] = Field(default_factory=list)
    error: ToolError | None = None
    raw_result_ref: str | None = None

    @model_validator(mode='after')
    def consistent(self) -> 'ToolResult':
        if self.ok == (self.error is not None) or (not self.ok and self.data is not None):
            raise ValueError('工具结果的成功、数据与错误字段不一致')
        return self

    @classmethod
    def failure(cls, code: str, message: str, retryable: bool) -> 'ToolResult':
        return cls(ok=False, error=ToolError(code=code, message=message, retryable=retryable))


@dataclass(frozen=True)
class ToolDefinition(Generic[Args]):
    name: str
    description: str
    args_model: type[Args]
    read_only: bool
    handler: Callable[[Args, ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class RemoteToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    handler: Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolExecution:
    tool_call_id: str
    raw_result: ToolResult
    model_result: ToolResult

    def model_message(self) -> dict[str, str]:
        return {'role': 'tool', 'tool_call_id': self.tool_call_id,
                'content': self.model_result.model_dump_json()}


def limit_model_result(result: ToolResult, max_chars: int = MODEL_RESULT_MAX_CHARS) -> ToolResult:
    return ToolResult.model_validate(truncate_result(result.model_dump(mode='json'), max_chars))
