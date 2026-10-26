import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
import openai
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ErrorInfo(BaseModel):
    code: str
    retryable: bool
    message: str
    provider_request_id: str | None


class AdapterError(Exception):
    def __init__(self, code: str, retryable: bool, message: str, provider_request_id: str | None):
        self.info = ErrorInfo(code=code, retryable=retryable, message=message, provider_request_id=provider_request_id)
        super().__init__(message)


class Sanitizer:
    def __init__(self, secrets: list[str]):
        self.secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", value)
        value = re.sub(r"https?://[^\s\"'<>]+", self._url, value)
        return value

    @staticmethod
    def _url(match: re.Match) -> str:
        url = urlsplit(match.group())
        return urlunsplit((url.scheme, url.hostname or "", url.path, "", ""))

    def clean(self, data: Any) -> Any:
        value = data
        if isinstance(value, BaseModel):
            return self.clean(value.model_dump(mode="json"))
        if isinstance(value, dict):
            return {str(k): "[REDACTED]" if re.search(r"(?i)(authorization|api.?key|secret|password|^token$|access_token|refresh_token|file_urls|full_zip_url)", str(k))
                    else self.argument_text(v) if k in ('arguments', 'raw_arguments') and isinstance(v, str)
                    else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            if value.lstrip().startswith(('{', '[')):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    return self.text(value)
                return json.dumps(self.clean(parsed), ensure_ascii=False)
            return self.text(value)
        return value

    def argument_text(self, value: str) -> str:
        try:
            parsed = json.loads(value)
        except ValueError:
            return '[INVALID_JSON]'
        return json.dumps(self.clean(parsed), ensure_ascii=False)


class Usage(BaseModel):
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    total_tokens: int | None = Field(None, ge=0)
    details: dict[str, Any]

    def observation(self) -> dict[str, int]:
        return {key: value for key, value in {"input": self.input_tokens, "output": self.output_tokens, "total": self.total_tokens}.items() if value is not None}


def parse_usage(value: Any) -> Usage | None:
    if value is None:
        return None
    data = value.model_dump(exclude_none=True) if isinstance(value, BaseModel) else value
    if not data:
        return None
    return Usage(input_tokens=data.get("prompt_tokens", data.get("input_tokens")), output_tokens=data.get("completion_tokens", data.get("output_tokens")), total_tokens=data.get("total_tokens"), details=data)


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] | None
    raw_arguments: str


class ChatEvent(BaseModel):
    type: Literal["content", "tool_call", "usage", "done"]
    content: str | None = None
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    model: str | None = None
    provider_request_id: str | None = None


class EmbeddingResult(BaseModel):
    vectors: list[list[float]]
    dimension: int = Field(gt=0)
    model: str
    usage: Usage | None
    provider_request_id: str | None


class RankItem(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, strict=True)
    index: int = Field(ge=0)
    score: float


class MinerUResult(BaseModel):
    markdown: str = Field(min_length=1)
    metadata: dict[str, Any]
    model: str
    batch_id: str
    provider_request_id: str | None


def request_id(headers: Any) -> str | None:
    return headers.get("x-siliconcloud-trace-id") or headers.get("x-request-id") or headers.get("request-id")


def normalize_error(exc: Exception, sanitizer: Sanitizer, provider_id: str | None) -> AdapterError:
    if isinstance(exc, AdapterError):
        info = exc.info
        return AdapterError(info.code, info.retryable, sanitizer.text(info.message), info.provider_request_id or provider_id)
    if isinstance(exc, (httpx.TimeoutException, openai.APITimeoutError, TimeoutError)):
        return AdapterError("timeout", True, "服务请求超时", provider_id)
    if isinstance(exc, (httpx.TransportError, openai.APIConnectionError)):
        return AdapterError("connection_error", True, "服务连接失败", provider_id)
    if isinstance(exc, (httpx.HTTPStatusError, openai.APIStatusError)):
        response = exc.response
        status = response.status_code
        provider_id = request_id(response.headers) or provider_id
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            code = str(error.get("code") or f"http_{status}")
            message = str(error.get("message") or error.get("msg") or f"服务返回 HTTP {status}")
        else:
            code, message = f"http_{status}", f"服务返回 HTTP {status}"
        return AdapterError(code, status in {408, 429} or status >= 500, sanitizer.text(message), provider_id)
    if isinstance(exc, (ValidationError, ValueError, KeyError, TypeError)):
        return AdapterError("invalid_response", False, "服务响应结构或内容无效", provider_id)
    return AdapterError("adapter_error", False, "适配器失败：" + type(exc).__name__, provider_id)
