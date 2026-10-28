import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.adapters.common import AdapterError, ChatEvent
from app.agent import ChatAgent, SYSTEM_PROMPT
from app.settings import Settings


@pytest.fixture
def chat_app(monkeypatch):
    instances = []
    observer = SimpleNamespace(client=SimpleNamespace(flush=Mock()))

    class FakeChat:
        def __init__(self, settings, observation):
            self.model = settings.CHAT_MODEL
            self.events = [ChatEvent(type="content", content="你好\n"), ChatEvent(type="content", content="世界"), ChatEvent(type="done")]
            self.received = None
            self.resume = None
            self.stream_closed = False
            self.closed = False
            instances.append(self)

        async def stream(self, messages, tools, tool_choice, options):
            self.received = (messages, tools, tool_choice, options)
            try:
                for index, event in enumerate(self.events):
                    if isinstance(event, Exception):
                        raise event
                    yield event
                    if index == 0 and self.resume is not None:
                        await self.resume.wait()
            finally:
                self.stream_closed = True

        async def close(self):
            self.closed = True

    monkeypatch.setattr(main, "load_settings", lambda: Settings(_env_file=None, CHAT_BASE_URL="https://chat.invalid/v1", CHAT_API_KEY="test-key", CHAT_MODEL="test-model", JWT_SECRET='isolated-test-secret'))
    monkeypatch.setattr(main, 'initialize_admin', lambda: None)
    main.app.dependency_overrides[main.require_origin] = lambda: None
    main.app.dependency_overrides[main.current_user] = lambda: 'isolated-chat-user'
    monkeypatch.setattr(main, "Observer", lambda settings: observer)
    def fake_chat(settings, observation):
        return instances[0] if instances else FakeChat(settings, observation)
    monkeypatch.setattr(main, "ChatAdapter", fake_chat)
    adapter = fake_chat(main.load_settings(), observer)
    with TestClient(main.app) as client:
        client.headers['origin'] = 'http://127.0.0.1:8000'
        yield client, instances[0]
    main.app.dependency_overrides.clear()
    if adapter.received is not None:
        assert adapter.stream_closed
    observer.client.flush.assert_called_once()


def test_chat_model_name_only(chat_app):
    client, adapter = chat_app
    response = client.get("/api/chat/model")
    assert response.status_code == 200
    assert response.json() == {"model": adapter.model}
    assert adapter.received is None


def test_quick_answer_uses_original_homepage(chat_app):
    client, _ = chat_app
    response = client.get('/quick-answer', follow_redirects=False)
    assert response.status_code == 307
    assert response.headers['location'] == '/'
    homepage = client.get('/').text
    assert 'value="smart_reasoning" selected' in homepage
    assert 'href="/settings"' in homepage
    assert '/static/quick-answer.js' in homepage
    assert '/static/quick-answer.css' in homepage
    assert client.get('/static/quick-answer.js').status_code == 200
    assert client.get('/static/quick-answer.css').status_code == 200


def test_chat_identity_context_and_protocol(chat_app):
    client, adapter = chat_app
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好，有什么问题？"}, {"role": "user", "content": "继续"}]
    response = client.post("/api/chat", json={"messages": history})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-cache"
    assert [json.loads(line) for line in response.iter_lines()] == [{"type": "content", "content": "你好\n"}, {"type": "content", "content": "世界"}, {"type": "done"}]
    assert adapter.received == ([{"role": "system", "content": SYSTEM_PROMPT}, *history], None, None, {})
    assert adapter.stream_closed
    for path in ["/", "/static/app.js", "/static/styles.css", "/docs", "/openapi.json"]:
        assert client.get(path).status_code == 200


@pytest.mark.parametrize("messages", [[], [{"role": "system", "content": "替换身份"}], [{"role": "user", "content": " "}], [{"role": "tool", "content": "执行工具"}]])
def test_chat_rejects_invalid_messages(chat_app, messages):
    client, adapter = chat_app
    assert client.post("/api/chat", json={"messages": messages}).status_code == 422
    assert adapter.received is None


def test_chat_error_preserves_partial_output_without_done(chat_app):
    client, adapter = chat_app
    adapter.events = [ChatEvent(type="content", content="部分回复"), AdapterError("timeout", True, "服务请求超时", None)]
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "你好"}]})
    assert [json.loads(line) for line in response.iter_lines()] == [{"type": "content", "content": "部分回复"}, {"type": "error", "message": "服务请求超时"}]
    assert adapter.stream_closed


@pytest.mark.asyncio
async def test_asgi_sends_first_chunk_before_generation_finishes(chat_app):
    _, adapter = chat_app
    adapter.resume = asyncio.Event()
    body = json.dumps({"messages": [{"role": "user", "content": "你好"}]}).encode()
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/chat", "raw_path": b"/api/chat", "query_string": b"", "root_path": "", "headers": [(b"content-type", b"application/json"), (b"origin", b"http://127.0.0.1:8000")], "server": ("test", 80), "client": ("test", 1234)}
    events = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body" and message["body"]:
            events.append(json.loads(message["body"]))
            if len(events) == 1:
                assert not adapter.resume.is_set()
                adapter.resume.set()

    await asyncio.wait_for(main.app(scope, receive, send), timeout=2)
    assert [event["type"] for event in events] == ["content", "content", "done"]
    assert adapter.stream_closed


@pytest.mark.asyncio
async def test_closing_reply_stream_closes_model_stream(chat_app):
    _, adapter = chat_app
    events = main.stream_reply(ChatAgent(adapter), [{"role": "user", "content": "你好"}])
    assert json.loads(await anext(events))["type"] == "content"
    await events.aclose()
    assert adapter.stream_closed
