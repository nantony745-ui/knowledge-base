import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from app.adapters.common import AdapterError, ChatEvent, ToolCall, normalize_error, parse_usage, request_id
from app.adapters.observation import Observer, observed_tool_call
from app.settings import Settings


class ToolBuffer:
    def __init__(self):
        self.calls: dict[int, dict[str, str]] = {}

    def append(self, calls: list[Any]) -> None:
        for call in calls:
            item = self.calls.setdefault(call.index, {"id": "", "name": "", "arguments": ""})
            item["id"] += call.id or ""
            if call.function:
                item["name"] += call.function.name or ""
                item["arguments"] += call.function.arguments or ""

    def complete(self, finish_reason: str | None, defer_tool_argument_validation: bool = False) -> list[ToolCall]:
        if self.calls and finish_reason != "tool_calls":
            raise AdapterError("incomplete_tool_call", False, "工具调用未完整结束", None)
        result = []
        for index in sorted(self.calls):
            item = self.calls[index]
            arguments = None if defer_tool_argument_validation else json.loads(item["arguments"])
            result.append(ToolCall(id=item["id"], name=item["name"], arguments=arguments, raw_arguments=item["arguments"]))
        return result


class ChatAdapter:
    def __init__(self, settings: Settings, observer: Observer):
        settings.require("CHAT_BASE_URL", "CHAT_API_KEY", "CHAT_MODEL")
        self.model = settings.CHAT_MODEL
        self.is_ark = urlsplit(settings.CHAT_BASE_URL).hostname.endswith('.volces.com')
        self.observer = observer
        self.client = AsyncOpenAI(base_url=settings.CHAT_BASE_URL, api_key=settings.CHAT_API_KEY.get_secret_value(), max_retries=0, timeout=settings.timeout("CHAT", True))

    async def stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, tool_choice: Any, options: dict[str, Any], *, defer_tool_argument_validation: bool = False) -> AsyncIterator[ChatEvent]:
        if self.is_ark and 'enable_thinking' in options.get('extra_body', {}):
            extra_body = dict(options['extra_body'])
            extra_body['thinking'] = {'type': 'enabled' if extra_body.pop('enable_thinking') else 'disabled'}
            options = {**options, 'extra_body': extra_body}
        provider_id = None
        with self.observer.observe("chat", self.model, {"messages": messages, "tools": tools,
                "tool_choice": tool_choice, "options": options}) as observation:
            observation.model_parameters = {key: options[key] for key in
                ('max_tokens', 'temperature', 'top_p', 'seed', 'stop') if key in options}
            buffer, text, finish_reason = ToolBuffer(), [], None
            try:
                kwargs = {"tools": tools, "tool_choice": tool_choice} if tools else {}
                raw = await self.client.chat.completions.with_raw_response.create(model=self.model, messages=messages, stream=True, **kwargs, **options)
                provider_id = request_id(raw.headers)
                observation.provider_request_id = provider_id
                stream = raw.parse()
                async with stream:
                    async for chunk in stream:
                        if chunk.usage:
                            observation.usage = parse_usage(chunk.usage)
                            yield ChatEvent(type="usage", usage=observation.usage, provider_request_id=provider_id)
                        for choice in chunk.choices:
                            if choice.index != 0:
                                continue
                            if choice.delta.content:
                                observation.first_output('content')
                                text.append(choice.delta.content)
                                yield ChatEvent(type="content", content=choice.delta.content)
                            if choice.delta.tool_calls:
                                if any(call.id or (call.function and (call.function.name or call.function.arguments))
                                       for call in choice.delta.tool_calls):
                                    observation.first_output('tool_call')
                                buffer.append(choice.delta.tool_calls)
                            if choice.finish_reason:
                                finish_reason = choice.finish_reason
                if finish_reason not in {"stop", "tool_calls"}:
                    message = (f"模型输出达到长度上限（{options.get('max_tokens', '默认')} tokens），回答未完成"
                               if finish_reason == 'length' else "流未正常完成：" + str(finish_reason))
                    raise AdapterError("incomplete_stream", False, message, provider_id)
                calls = buffer.complete(finish_reason, defer_tool_argument_validation)
                observation.provider_request_id = provider_id
                observation.output = {"content": "".join(text), "tool_calls": [observed_tool_call(call) for call in calls], "finish_reason": finish_reason}
                for call in calls:
                    yield ChatEvent(type="tool_call", tool_call=call)
                yield ChatEvent(type="done", finish_reason=finish_reason, model=self.model, provider_request_id=provider_id)
            except Exception as exc:
                raise normalize_error(exc, self.observer.sanitizer, provider_id) from None
            finally:
                if observation.output is None:
                    observation.output = {'content': ''.join(text), 'finish_reason': finish_reason}
                    observation.metadata['partial_tool_call_count'] = len(buffer.calls)

    async def close(self) -> None:
        await self.client.close()
