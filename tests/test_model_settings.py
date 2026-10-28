import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.adapters.common import AdapterError, ChatEvent
from app.api import model_settings as api
from app.api.auth import current_user
from app.main import adapter_error, configured_reply, request_validation_error
from app.services import model_settings as service
from app.settings import Settings, load_settings


@pytest.fixture
def model_client(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, 'MODEL_SETTINGS_FILE', tmp_path / '.env.models')
    for prefix in ('CHAT', 'EMBED', 'RERANK'):
        monkeypatch.setenv(prefix + '_MODEL', prefix.lower() + '-default')
        monkeypatch.setenv(prefix + '_API_KEY', prefix.lower() + '-default-secret')
    monkeypatch.setenv('CHAT_BASE_URL', 'https://chat.invalid/v1')
    monkeypatch.setenv('EMBED_BASE_URL', 'https://embedding.invalid/v1')
    monkeypatch.setenv('RERANK_URL', 'https://rerank.invalid/v1/rerank')
    monkeypatch.setenv('APP_ORIGIN', 'http://testserver')
    monkeypatch.setenv('JWT_SECRET', 'isolated-model-settings-test-secret')
    application = FastAPI()
    application.include_router(api.router)
    application.add_exception_handler(AdapterError, adapter_error)
    application.add_exception_handler(RequestValidationError, request_validation_error)
    def owner(request: Request):
        if request.cookies.get('test_admin') != 'yes':
            raise HTTPException(401)
        return 'owner'
    application.dependency_overrides[current_user] = owner
    with TestClient(application, headers={'Origin': 'http://testserver'}) as client:
        client.cookies.set('test_admin', 'yes')
        yield client


def body(**values):
    return {'model': 'replacement', 'url': 'https://replacement.invalid/v1', **values}


def test_current_defaults_and_private_persistent_independent_configuration(model_client):
    client = model_client
    initial = client.get('/api/settings/models')
    assert initial.status_code == 200
    assert initial.json()['chat'] == {'model': 'chat-default', 'url': 'https://chat.invalid/v1', 'key_configured': True}
    assert 'secret' not in initial.text
    before = load_settings()
    response = client.put('/api/settings/models/chat', json=body(api_key='replacement-secret'))
    assert response.status_code == 200 and response.json()['key_configured']
    assert 'replacement-secret' not in response.text
    after = load_settings()
    assert after.CHAT_MODEL == 'replacement'
    assert after.CHAT_API_KEY.get_secret_value() == 'replacement-secret'
    assert after.EMBED_MODEL == 'embed-default' and after.RERANK_MODEL == 'rerank-default'
    assert before.CHAT_MODEL == 'chat-default'
    assert Settings.MODEL_SETTINGS_FILE.stat().st_mode & 0o777 == 0o600
    client.put('/api/settings/models/chat', json=body(model='next')).raise_for_status()
    assert load_settings().CHAT_API_KEY.get_secret_value() == 'replacement-secret'
    client.put('/api/settings/models/embedding', json=body(url='https://embedding.invalid/v1/embeddings')).raise_for_status()
    assert load_settings().EMBED_BASE_URL == 'https://embedding.invalid/v1'
    assert load_settings().CHAT_MODEL == 'next'
    assert client.get('/api/settings/models').json()['chat']['model'] == 'next'


def test_auth_origin_and_invalid_inputs_never_echo_secrets(model_client):
    client = model_client
    client.cookies.clear()
    for method, path in [('get', '/api/settings/models'), ('put', '/api/settings/models/chat'),
                         ('post', '/api/settings/models/chat/test')]:
        options = {} if method == 'get' else {'json': body(api_key='private-secret')}
        assert getattr(client, method)(path, **options).status_code == 401
    client.cookies.set('test_admin', 'yes')
    for method, path in [('put', '/api/settings/models/chat'), ('post', '/api/settings/models/chat/test')]:
        assert getattr(client, method)(path, headers={'Origin': 'https://other.invalid'}, json=body()).status_code == 403
    cases = [body(model=''), body(model='name\nInjected=x'), body(api_key='private-secret\r\nInjected=x'),
             body(api_key='Bearer private-secret'), body(extra='private-secret')]
    for value in cases:
        response = client.put('/api/settings/models/chat', json=value)
        assert response.status_code == 422 and 'private-secret' not in response.text
    for url in ('file:///etc/passwd', 'https://user:private-secret@example.invalid/v1', 'https://example.invalid?key=private-secret'):
        response = client.put('/api/settings/models/chat', json=body(url=url))
        assert response.status_code == 422 and 'private-secret' not in response.text
    assert client.put('/api/settings/models/unknown', json=body()).status_code == 422
    assert not Settings.MODEL_SETTINGS_FILE.exists()


def test_atomic_save_failure_preserves_old_configuration(model_client, monkeypatch):
    client = model_client
    client.put('/api/settings/models/chat', json=body()).raise_for_status()
    original = Settings.MODEL_SETTINGS_FILE.read_bytes()
    def fail_replace(*args):
        raise OSError('private-error')
    monkeypatch.setattr(service.os, 'replace', fail_replace)
    response = client.put('/api/settings/models/chat', json=body(model='not-saved'))
    assert response.status_code == 503 and 'private-error' not in response.text
    assert Settings.MODEL_SETTINGS_FILE.read_bytes() == original
    assert load_settings().CHAT_MODEL == 'replacement'
    assert list(Settings.MODEL_SETTINGS_FILE.parent.iterdir()) == [Settings.MODEL_SETTINGS_FILE]


@pytest.mark.parametrize('kind', ['chat', 'embedding', 'reranker'])
def test_probe_uses_unsaved_values_retains_key_and_closes_client(model_client, monkeypatch, kind):
    seen = []
    class Adapter:
        def __init__(self, settings, observer):
            seen.append(settings)
        async def stream(self, *args):
            yield ChatEvent(type='content', content='OK')
            yield ChatEvent(type='done')
        async def embed(self, texts):
            return SimpleNamespace(dimension=4096)
        async def rerank(self, *args):
            return [{'index': 0, 'score': 1.0}]
        close = AsyncMock()
    monkeypatch.setattr(api, {'chat': 'ChatAdapter', 'embedding': 'EmbeddingAdapter', 'reranker': 'RerankAdapter'}[kind], Adapter)
    response = model_client.post(f'/api/settings/models/{kind}/test', json=body())
    assert response.status_code == 200 and response.json()['connected'] is True
    assert len(seen) == 1
    model, url, key = service.MODEL_FIELDS[kind]
    assert getattr(seen[0], model) == 'replacement'
    assert getattr(seen[0], key).get_secret_value().endswith('-default-secret')
    Adapter.close.assert_awaited_once()
    assert not Settings.MODEL_SETTINGS_FILE.exists()


def test_probe_failure_is_visible_and_does_not_save(model_client, monkeypatch):
    close = AsyncMock()
    adapter = SimpleNamespace(embed=AsyncMock(side_effect=AdapterError('provider_http_error', False, '认证失败', None)), close=close)
    monkeypatch.setattr(api, 'EmbeddingAdapter', lambda *args: adapter)
    response = model_client.post('/api/settings/models/embedding/test', json=body(api_key='unsaved-secret'))
    assert response.status_code == 503 and response.json()['detail']['message'] == '认证失败'
    assert 'unsaved-secret' not in response.text
    close.assert_awaited_once()
    assert not Settings.MODEL_SETTINGS_FILE.exists()


@pytest.mark.asyncio
async def test_plain_chat_new_requests_use_saved_configuration(model_client, monkeypatch):
    seen = []
    closed = []
    class Chat:
        def __init__(self, settings, observer):
            self.model = settings.CHAT_MODEL
            seen.append((self.model, settings.CHAT_API_KEY.get_secret_value()))
        async def stream(self, *args):
            yield ChatEvent(type='content', content=self.model)
            yield ChatEvent(type='done')
        async def close(self):
            closed.append(self.model)
    monkeypatch.setattr('app.main.ChatAdapter', Chat)
    monkeypatch.setattr('app.main.Observer', lambda *_: None)
    first = load_settings()
    model_client.put('/api/settings/models/chat', json=body(api_key='new-secret')).raise_for_status()
    for settings in (first, load_settings()):
        assert [event async for event in configured_reply(settings, [{'role': 'user', 'content': '测试'}])]
    assert seen == [('chat-default', 'chat-default-secret'), ('replacement', 'new-secret')]
    assert closed == ['chat-default', 'replacement']


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['quick_answer', 'smart_reasoning'])
async def test_active_run_keeps_runtime_and_next_run_uses_new_runtime(model_client, monkeypatch, source_store, mode):
    from app.services.runs import RunManager
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []
    class Pipeline:
        def __init__(self, settings):
            self.settings = settings
        def freeze_skills(self, run):
            pass
        async def run(self, *args):
            seen.append((self.settings.CHAT_MODEL, self.settings.CHAT_API_KEY.get_secret_value()))
            entered.set()
            await release.wait()
            return SimpleNamespace(text='OK', citations=[])
    monkeypatch.setattr('app.services.runs.check_session', lambda *args: None)
    monkeypatch.setattr('app.services.runs.authorize', lambda *args: [])
    monkeypatch.setattr('app.agent.history.load_history', lambda *args: [])
    monkeypatch.setattr('app.services.runs.repair_tool_results', lambda *args: [])
    monkeypatch.setattr('app.services.runs.append_run_message', lambda *args: {'id': str(uuid4())})
    manager = RunManager(load_settings(), None, None)
    def runtime():
        settings = load_settings()
        pipeline = Pipeline(settings)
        return settings, pipeline, pipeline, None
    manager.runtime_factory = runtime
    first = await manager.create(str(uuid4()), 'owner', '测试', [], mode)
    await asyncio.wait_for(entered.wait(), 2)
    model_client.put('/api/settings/models/chat', json=body(api_key='rotated-secret')).raise_for_status()
    second = await manager.create(str(uuid4()), 'owner', '测试', [], mode)
    release.set()
    await asyncio.gather(manager.runs[first].task, manager.runs[second].task)
    assert seen == [('chat-default', 'chat-default-secret'), ('replacement', 'rotated-secret')]
    assert all(run.terminal and run.events[-1]['type'] == 'run.completed' for run in manager.runs.values())


@pytest.fixture
def local_provider(monkeypatch):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    monkeypatch.setenv('EMBED_DIMENSION', '2')
    requests = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append((self.path, self.headers['Authorization'], data))
            status, kind = 200, 'application/json'
            if self.headers['Authorization'] != 'Bearer valid-local-key':
                status = 401
                payload = json.dumps({'error': {'message': self.headers['Authorization']}})
            elif self.path.endswith('/chat/completions'):
                kind = 'text/event-stream'
                frames = [{'id': 'probe', 'object': 'chat.completion.chunk', 'created': 1, 'model': data['model'],
                           'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
                          for delta, finish in [({'content': 'OK'}, None), ({}, 'stop')]]
                payload = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'
            elif self.path.endswith('/embeddings'):
                payload = json.dumps({'object': 'list', 'model': data['model'], 'data': [
                    {'object': 'embedding', 'index': 0, 'embedding': [0.1, 0.2]}],
                    'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
            else:
                payload = json.dumps({'results': [{'index': 0, 'relevance_score': 0.99}]})
            encoded = payload.encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('kind', ['chat', 'embedding', 'reranker'])
@pytest.mark.parametrize('valid_key', [True, False])
def test_real_adapters_over_local_http(model_client, local_provider, kind, valid_key):
    url, requests = local_provider
    key = 'valid-local-key' if valid_key else 'invalid-local-secret'
    if kind == 'reranker':
        url += '/custom/rerank'
    response = model_client.post(f'/api/settings/models/{kind}/test', json=body(url=url, api_key=key))
    assert response.status_code == (200 if valid_key else 503), response.text
    if valid_key:
        assert response.json()['connected'] is True
    else:
        assert response.json()['detail']['code'] == 'http_401'
        assert key not in response.text
    assert len(requests) == 1
    assert requests[0][1] == 'Bearer ' + key
    assert requests[0][2]['model'] == 'replacement'
    if kind == 'reranker':
        assert requests[0][0] == '/v1/custom/rerank'
    assert not Settings.MODEL_SETTINGS_FILE.exists()


def test_persist_key_literal_characters_without_interpolation(model_client):
    key = 'local-\\literal-"quoted\'-${NOT_AN_ENV_VAR}'
    response = model_client.put('/api/settings/models/chat', json=body(api_key=key))
    assert response.status_code == 200
    assert load_settings().CHAT_API_KEY.get_secret_value() == key


def test_settings_page_and_assets_are_served(model_client):
    from app.main import app
    application = FastAPI()
    application.router.routes.extend(app.router.routes)
    with TestClient(application) as client:
        assert client.get('/settings').status_code == 200
        assert '<span>Skills 与 MCP</span>' in client.get('/').text
        assert 'href="/settings"' in client.get('/').text
        for page in ('/skills', '/mcp-services'):
            assert 'Skills 与 MCP</h1>' in client.get(page).text
        for asset in ('settings.js', 'settings.css'):
            assert client.get('/static/' + asset).status_code == 200


def test_lifespan_runtime_wires_saved_models_into_all_consumers(model_client, monkeypatch, tmp_path):
    import app.main as main
    from app.adapters.observation import NullTraceClient
    monkeypatch.setattr(main, 'initialize_admin', lambda: None)
    monkeypatch.setattr(main, 'Observer', lambda *_: SimpleNamespace(client=NullTraceClient()))
    monkeypatch.setattr(main, 'load_settings', lambda: load_settings().model_copy(update={
        'SKILL_DIRS': (), 'DATA_DIR': tmp_path, 'LANGFUSE_PUBLIC_KEY': None, 'LANGFUSE_SECRET_KEY': None}))
    with TestClient(main.app):
        original = main.app.state.runs.runtime_factory()
        for kind in ('chat', 'embedding', 'reranker'):
            model_client.put('/api/settings/models/' + kind, json=body(api_key=kind + '-rotated')).raise_for_status()
        current, pipeline, engine, _ = main.app.state.runs.runtime_factory()
        assert current.CHAT_MODEL == current.EMBED_MODEL == current.RERANK_MODEL == 'replacement'
        assert current.CHAT_API_KEY.get_secret_value() == 'chat-rotated'
        assert pipeline.settings is current and pipeline.retrieval.settings is current
        assert engine.settings is current and engine.registry is not original[2].registry
        assert engine.skills is main.app.state.skills
        assert pipeline.retrieval.cache is main.app.state.retrieval.cache
        assert original[0].CHAT_MODEL == original[1].settings.CHAT_MODEL == original[2].settings.CHAT_MODEL == 'chat-default'


def test_timeout_and_empty_chat_are_not_reported_as_success(model_client, monkeypatch):
    adapter = SimpleNamespace(embed=AsyncMock(side_effect=TimeoutError), close=AsyncMock())
    monkeypatch.setattr(api, 'EmbeddingAdapter', lambda *args: adapter)
    response = model_client.post('/api/settings/models/embedding/test', json=body())
    assert response.status_code == 503 and response.json()['detail']['code'] == 'model_test_timeout'
    adapter.close.assert_awaited_once()
    async def empty_stream(*args):
        yield ChatEvent(type='done')
    chat = SimpleNamespace(stream=empty_stream, close=AsyncMock())
    monkeypatch.setattr(api, 'ChatAdapter', lambda *args: chat)
    response = model_client.post('/api/settings/models/chat/test', json=body())
    assert response.status_code == 503 and response.json()['detail']['code'] == 'empty_answer'
    chat.close.assert_awaited_once()
