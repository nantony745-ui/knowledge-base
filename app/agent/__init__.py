from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from app.adapters.chat import ChatAdapter

SYSTEM_PROMPT = (
    "你是 Knowledge Base 的知识助手，帮助用户理解问题、整理思路和解答疑问。"
    "默认使用中文，回答准确、清晰、简洁；不确定的内容应明确说明，不编造事实。"
    "你只能使用当前对话提供的信息与自身知识，不要声称已检索知识库、读取文件或执行工具。"
)


class ChatAgent:
    def __init__(self, chat: ChatAdapter):
        self.chat = chat

    async def stream(self, messages: list[dict[str, str]]) -> AsyncIterator[dict[str, Any]]:
        context = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
        async with aclosing(self.chat.stream(context, None, None, {})) as events:
            async for event in events:
                if event.type in {"content", "done"}:
                    yield event.model_dump(include={"type", "content"}, exclude_none=True)
