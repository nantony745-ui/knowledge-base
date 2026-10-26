import asyncio
import copy
import csv
import json
import subprocess
import sys
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.adapters.common import AdapterError, ChatEvent, ToolCall
from app.agent.context import ContextController, TokenEstimator, freeze_config
from app.agent.engine import ReasoningEngine
from app.agent.messages import protocol_complete
from app.agent.sources import RunSources
from app.agent.tools import ToolContext, ToolRegistry, create_knowledge_registry
from app.agent.tools.skills import SKILL_TOOLS, register_skill_tools
from app.api.auth import current_user
from app.api.skills import router
from app.sandbox.skills import SkillCatalog
from app.services.runs import Run, RunManager
from app.settings import ROOT, Settings


def skill_directory(root, name, body, reference):
    directory = root / name
    (directory / 'references').mkdir(parents=True)
    (directory / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: 简介{name}\n---\n{body}', encoding='utf-8')
    (directory / 'references/format.md').write_text(reference, encoding='utf-8')
    return directory


@pytest.fixture
def catalog(tmp_path):
    return SkillCatalog((skill_directory(tmp_path, 'text-report', '技能正文标记 [s1]', '参考正文标记'),))


def tool_call(name, arguments, call_id):
    return ChatEvent(type='tool_call', tool_call=ToolCall(id=call_id, name=name, arguments=None,
        raw_arguments=json.dumps(arguments, ensure_ascii=False)))


class RecordingChat:
    def __init__(self, rounds):
        self.rounds, self.requests, self.schemas = rounds, [], []
        self.closed = False

    async def stream(self, messages, tools, choice, options, **kwargs):
        assert protocol_complete(messages)
        self.requests.append(copy.deepcopy(messages))
        self.schemas.append(copy.deepcopy(tools))
        for event in self.rounds[len(self.requests) - 1]:
            yield event
        yield ChatEvent(type='done')

    async def close(self):
        self.closed = True


@pytest.fixture
def runtime(tmp_path, catalog, monkeypatch, source_store):
    settings = Settings(_env_file=None, DATA_DIR=tmp_path / 'data', SKILL_DIRS=(catalog.entries['text-report'].directory,))
    registry = create_knowledge_registry(settings, None, None)
    engine = ReasoningEngine(settings, registry)
    run = Run(str(uuid4()), str(uuid4()), str(uuid4()), turn_id=str(uuid4()), mode='smart_reasoning')
    run.sources = RunSources(run.id, run.owner, [])
    rows = [{'seq': 1, 'id': run.turn_id, 'turn_id': run.turn_id, 'role': 'user', 'content': '统计',
             'status': 'complete', 'tool_calls': None}]
    def append(sid, owner, role, content, turn, status, citations, rid, calls, call_id):
        row = {'seq': len(rows) + 1, 'id': str(uuid4()), 'turn_id': turn, 'role': role, 'content': content,
               'status': status, 'tool_calls': calls, 'tool_call_id': call_id, 'citations': citations}
        rows.append(row)
        return row
    async def context(owner, sid, rid, kb_ids, allowed, cancel, refs):
        return ToolContext(owner, sid, rid, frozenset(kb_ids), frozenset(allowed), cancel, refs)
    monkeypatch.setattr('app.agent.engine.append_run_message', append)
    monkeypatch.setattr('app.agent.engine.create_tool_context', context)
    monkeypatch.setattr('app.agent.engine.knowledge_scope', lambda context: [])
    monkeypatch.setattr('app.db.mcp_repository.list_services', lambda owner: [])
    return settings, registry, engine, run, rows, append


@pytest.mark.asyncio
async def test_progressive_loading_sources_and_independent_run(runtime, monkeypatch):
    settings, native, engine, run, rows, _ = runtime
    rounds = [[tool_call('list_skills', {}, 'list')],
        [tool_call('load_skill', {'name': 'text-report'}, 'load')],
        [tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/format.md'}, 'read')],
        [tool_call('load_skill', {'name': 'text-report'}, 'reload')],
        [ChatEvent(type='content', content='统计流程[s1]')]]
    chat = RecordingChat(rounds)
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat)
    answer = await engine.run('统计', [], run, [], None)
    initial = json.dumps(chat.requests[0], ensure_ascii=False)
    assert '简介text-report' in initial and '技能正文标记' not in initial and '参考正文标记' not in initial
    assert '技能正文标记' not in json.dumps(chat.requests[1], ensure_ascii=False)
    assert '技能正文标记' in json.dumps(chat.requests[2], ensure_ascii=False)
    assert '参考正文标记' not in json.dumps(chat.requests[2], ensure_ascii=False)
    assert '参考正文标记' in json.dumps(chat.requests[3], ensure_ascii=False)
    assert run.loaded_skills == {'text-report'} and answer.text == '统计流程' and answer.citations == []
    assert not run.sources.registered and chat.closed
    assert SKILL_TOOLS <= {schema['function']['name'] for schema in chat.schemas[0]}
    assert 'run_skill' not in {schema['function']['name'] for schema in chat.schemas[0]}
    assert not SKILL_TOOLS.intersection(native.definitions)
    loaded = json.loads(rows[4]['content'])
    assert loaded['model_result']['sources'] == [] and 'content' in loaded['model_result']['data']
    assert (settings.DATA_DIR / loaded['raw_result']['raw_result_ref']).is_file()
    second = Run(str(uuid4()), run.session_id, run.owner, turn_id=str(uuid4()))
    second.sources = RunSources(second.id, second.owner, [])
    chat2 = RecordingChat([[tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/format.md'}, 'read2')],
        [ChatEvent(type='content', content='需本轮加载')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat2)
    await engine.run('继续', [], second, rows, None)
    assert '技能正文标记' in json.dumps(chat2.requests[0], ensure_ascii=False)
    assert json.loads(chat2.requests[1][-1]['content'])['error']['code'] == 'skill_not_loaded'
    assert second.loaded_skills == set() and run.loaded_skills == {'text-report'}


@pytest.mark.asyncio
async def test_enabled_snapshot_old_new_and_reenabled_runs(runtime, monkeypatch):
    settings, native, engine, run, rows, _ = runtime
    run.enabled_skills = engine.skills.enabled_names()
    engine.skills.set_enabled('text-report', False)
    chat = RecordingChat([[tool_call('load_skill', {'name': 'text-report'}, 'old_load')],
        [tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/format.md'}, 'old_read')],
        [ChatEvent(type='content', content='旧run可继续')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat)
    await engine.run('继续', [], run, [], None)
    assert run.loaded_skills == {'text-report'} and '参考正文标记' in json.dumps(chat.requests[2], ensure_ascii=False)
    disabled = Run(str(uuid4()), run.session_id, run.owner, turn_id=str(uuid4()))
    disabled.sources = RunSources(disabled.id, disabled.owner, [])
    blocked = RecordingChat([[tool_call('list_skills', {}, 'disabled_list')],
        [tool_call('load_skill', {'name': 'text-report'}, 'disabled_load')],
        [tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/format.md'}, 'disabled_read')],
        [ChatEvent(type='content', content='未启用')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: blocked)
    await engine.run('读取', [], disabled, [], None)
    assert disabled.enabled_skills == frozenset() and not disabled.loaded_skills
    assert '简介text-report' not in json.dumps(blocked.requests[0], ensure_ascii=False)
    assert json.loads(blocked.requests[1][-1]['content'])['data']['skills'] == []
    for request in blocked.requests[2:]:
        assert json.loads(request[-1]['content'])['error']['code'] == 'skill_disabled'
    engine.skills.set_enabled('text-report', True)
    registry = ToolRegistry(settings, lambda name, result: result)
    register_skill_tools(registry, engine.skills, disabled)
    context = ToolContext(disabled.owner, disabled.session_id, disabled.id, frozenset(), SKILL_TOOLS, disabled.cancel_event)
    result = await registry.execute('load_skill', '{"name":"text-report"}', 'still_disabled', context)
    assert result.model_result.error.code == 'skill_disabled'
    fresh = Run(str(uuid4()), run.session_id, run.owner, turn_id=str(uuid4()))
    fresh.sources = RunSources(fresh.id, fresh.owner, [])
    restored = RecordingChat([[tool_call('load_skill', {'name': 'text-report'}, 'fresh_load')],
        [ChatEvent(type='content', content='新run可使用')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: restored)
    await engine.run('读取', [], fresh, [], None)
    assert fresh.enabled_skills == frozenset({'text-report'}) and fresh.loaded_skills == {'text-report'}


@pytest.mark.asyncio
async def test_manager_freezes_skills_before_background_task(runtime, monkeypatch):
    settings, native, engine, _, _, _ = runtime
    manager = RunManager(settings, None, None, engine)
    monkeypatch.setattr('app.services.runs.check_session', lambda *args: None)
    monkeypatch.setattr('app.services.runs.authorize', lambda *args: [])
    monkeypatch.setattr('app.agent.history.load_history', lambda *args: [])
    monkeypatch.setattr('app.services.runs.repair_tool_results', lambda *args: [])
    def register(*args):
        engine.skills.set_enabled('text-report', False)
    monkeypatch.setattr('app.services.runs.register_run', register)
    monkeypatch.setattr('app.services.runs.append_run_message', lambda *args: {'id': str(uuid4()), 'seq': 1})
    observed = []
    async def execute(run, *args):
        observed.append(run.enabled_skills)
    monkeypatch.setattr(manager, 'execute', execute)
    rid = await manager.create(str(uuid4()), str(uuid4()), '读取技能', [], 'smart_reasoning')
    await manager.runs[rid].task
    assert observed == [frozenset({'text-report'})] and engine.skills.enabled_names() == frozenset()
    second = await manager.create(str(uuid4()), str(uuid4()), '读取技能', [], 'smart_reasoning')
    await manager.runs[second].task
    assert observed[-1] == frozenset()


@pytest.mark.asyncio
async def test_refresh_does_not_expand_running_agent(runtime, monkeypatch):
    settings, native, engine, run, rows, _ = runtime
    directory = engine.skills.entries['text-report'].directory
    engine.skills = SkillCatalog((directory.parent,), settings.DATA_DIR / 'skills/state.json')
    engine.freeze_skills(run)
    run.loaded_skills.add('text-report')
    (directory / 'references/new.md').write_text('新资源正文标记')
    skill_directory(directory.parent, 'beta', 'beta正文', 'beta参考')
    engine.skills.refresh()
    old = RecordingChat([[tool_call('list_skills', {}, 'old_list')],
        [tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/new.md'}, 'old_new_resource')],
        [tool_call('load_skill', {'name': 'beta'}, 'old_beta')],
        [ChatEvent(type='content', content='旧run保持原范围')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: old)
    await engine.run('查看技能', [], run, [], None)
    assert '简介beta' not in json.dumps(old.requests[0], ensure_ascii=False)
    listed = json.loads(old.requests[1][-1]['content'])['data']['skills']
    assert [item['name'] for item in listed] == ['text-report']
    assert 'references/new.md' not in {item['path'] for item in listed[0]['resources']}
    assert json.loads(old.requests[2][-1]['content'])['error']['code'] == 'unknown_skill_resource'
    assert json.loads(old.requests[3][-1]['content'])['error']['code'] == 'unknown_skill'
    fresh = Run(str(uuid4()), run.session_id, run.owner, turn_id=str(uuid4()))
    fresh.sources = RunSources(fresh.id, fresh.owner, [])
    new = RecordingChat([[tool_call('load_skill', {'name': 'text-report'}, 'new_load')],
        [tool_call('read_skill_resource', {'name': 'text-report', 'path': 'references/new.md'}, 'new_resource')],
        [ChatEvent(type='content', content='新run可见新目录')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: new)
    await engine.run('查看技能', [], fresh, [], None)
    assert '简介beta' in json.dumps(new.requests[0], ensure_ascii=False)
    assert '新资源正文标记' in json.dumps(new.requests[2], ensure_ascii=False)
    assert fresh.loaded_skills == {'text-report'} and fresh.enabled_skills == frozenset({'beta', 'text-report'})


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['archive', 'message', 'cancel_archive', 'cancel_message'])
async def test_loaded_state_requires_saved_success(runtime, monkeypatch, failure):
    settings, native, engine, run, rows, append = runtime
    original = ToolRegistry._persist
    def persist(self, name, identifier, result):
        if name == 'load_skill' and failure == 'archive':
            raise OSError('受控写入失败')
        original(self, name, identifier, result)
        if name == 'load_skill' and failure == 'cancel_archive':
            run.cancel_event.set()
    def save(*args):
        if args[2] == 'tool' and failure == 'message':
            raise RuntimeError('受控消息写入失败')
        row = append(*args)
        if args[2] == 'tool' and failure == 'cancel_message':
            run.cancel_event.set()
        return row
    monkeypatch.setattr(ToolRegistry, '_persist', persist)
    monkeypatch.setattr('app.agent.engine.append_run_message', save)
    chat = RecordingChat([[tool_call('load_skill', {'name': 'text-report'}, 'load')],
                          [ChatEvent(type='content', content='未加载')]])
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat)
    if failure == 'archive':
        await engine.run('统计', [], run, [], None)
        assert json.loads(rows[-1]['content'])['model_result']['error']['code'] == 'raw_result_write_failed'
    else:
        with pytest.raises(RuntimeError if failure == 'message' else asyncio.CancelledError):
            await engine.run('统计', [], run, [], None)
    assert not run.loaded_skills and chat.closed


@pytest.mark.parametrize('metadata', [
    '', 'name: text-report\n', 'name: text-report\ndescription: ""\n',
    'name: 123\ndescription: test\n', 'name: text-report\ndescription: true\n',
    'name: text-report\ndescription: test\nextra: bad\n',
    'name: one\nname: two\ndescription: test\n', 'name: ../bad\ndescription: test\n',
    'name: text-report\ndescription: !!python/object:bad {}\n',
    '<<: {name: text-report, description: test}\n', '[name, description]\n',
    '? [name, description]\n: value\n', 'name: [broken\n',
])
def test_invalid_metadata_fails_explicitly(tmp_path, metadata):
    directory = tmp_path / 'skill'
    directory.mkdir()
    (directory / 'SKILL.md').write_text('---\n' + metadata + '---\n正文', encoding='utf-8')
    with pytest.raises(AdapterError) as error:
        SkillCatalog((directory,))
    assert error.value.info.code == 'invalid_skill_metadata'


@pytest.mark.parametrize('text', ['无元数据', '---\nname: text-report\ndescription: 简介\n'])
def test_missing_or_unclosed_frontmatter(tmp_path, text):
    directory = tmp_path / 'skill'
    directory.mkdir()
    (directory / 'SKILL.md').write_text(text)
    with pytest.raises(AdapterError, match='frontmatter'):
        SkillCatalog((directory,))


def test_catalog_order_duplicate_and_no_body_discovery(tmp_path):
    zed = skill_directory(tmp_path, 'zed', '正文Z', '参考Z')
    alpha = skill_directory(tmp_path, 'alpha', '正文A', '参考A')
    (alpha / 'unregistered.bin').write_bytes(b'\x00')
    catalog = SkillCatalog((zed, alpha))
    assert [item.name for item in catalog.summaries()] == ['alpha', 'zed']
    assert catalog.summaries() == SkillCatalog((alpha, zed)).summaries()
    assert all('content' not in item.model_dump() for item in catalog.summaries())
    (alpha / 'SKILL.md').write_bytes(b'---\nname: alpha\ndescription: test\n---\n\xff')
    assert SkillCatalog((alpha,)).introductions() == [{'name': 'alpha', 'description': 'test'}]
    with pytest.raises(AdapterError):
        catalog.load('alpha')
    duplicate = tmp_path / 'duplicate'
    duplicate.mkdir()
    (duplicate / 'SKILL.md').write_text('---\nname: zed\ndescription: duplicate\n---\n正文')
    with pytest.raises(AdapterError) as error:
        SkillCatalog((zed, duplicate))
    assert error.value.info.code == 'duplicate_skill_name'


@pytest.mark.parametrize('path', ['/etc/passwd', 'C:/Windows/file.txt', '../outside.md',
    'references/../../outside.md', 'references/../SKILL.md', './references/format.md',
    'references//format.md', 'references\\format.md', 'references', 'missing.md', 'SKILL.md', ''])
def test_resource_boundaries(catalog, path):
    with pytest.raises(AdapterError) as error:
        catalog.read('text-report', path)
    assert error.value.info.code in {'unsafe_skill_path', 'unknown_skill_resource'}


def test_unknown_skill_resource_symlinks_and_metadata_change(catalog, tmp_path):
    for action in (lambda: catalog.load('unknown'), lambda: catalog.read('unknown', 'references/format.md')):
        with pytest.raises(AdapterError) as error:
            action()
        assert error.value.info.code == 'unknown_skill'
    directory = catalog.entries['text-report'].root
    (directory / 'new.md').write_text('没有登记的新资源')
    with pytest.raises(AdapterError) as error:
        catalog.read('text-report', 'new.md')
    assert error.value.info.code == 'unknown_skill_resource'
    outside = tmp_path / 'outside.md'
    outside.write_text('目录外标记')
    reference = directory / 'references/format.md'
    reference.unlink()
    reference.symlink_to(outside)
    with pytest.raises(AdapterError) as error:
        catalog.read('text-report', 'references/format.md')
    assert error.value.info.code == 'unsafe_skill_path'
    with pytest.raises(AdapterError):
        SkillCatalog((directory,))
    reference.unlink()
    reference.write_text('修复资源')
    skill = directory / 'SKILL.md'
    skill.write_text('---\nname: renamed\ndescription: 简介\n---\n正文')
    with pytest.raises(AdapterError) as error:
        catalog.load('text-report')
    assert error.value.info.code == 'skill_metadata_changed'
    skill.unlink()
    skill.symlink_to(outside)
    with pytest.raises(AdapterError) as error:
        catalog.load('text-report')
    assert error.value.info.code == 'unsafe_skill_path'


@pytest.mark.asyncio
async def test_tool_arguments_errors_and_budget(runtime, monkeypatch):
    settings, native, engine, run, rows, _ = runtime
    registry = ToolRegistry(settings, lambda name, result: result)
    register_skill_tools(registry, engine.skills, run)
    context = ToolContext(run.owner, run.session_id, run.id, frozenset(), SKILL_TOOLS, run.cancel_event)
    for index, (name, arguments, code) in enumerate([
        ('list_skills', {'name': 'text-report'}, 'invalid_arguments'),
        ('load_skill', {'name': 1}, 'invalid_arguments'),
        ('load_skill', {'name': 'missing'}, 'unknown_skill'),
        ('read_skill_resource', {'name': 'text-report', 'path': 'references/format.md'}, 'skill_not_loaded'),
    ]):
        execution = await registry.execute(name, json.dumps(arguments), f'bad_{index}', context)
        assert execution.model_result.error.code == code
    path = engine.skills.entries['text-report'].root / 'SKILL.md'
    path.write_text('---\nname: text-report\ndescription: 简介text-report\n---\n' + '长正文' * 10000)
    execution = await registry.execute('load_skill', '{"name":"text-report"}', 'long', context)
    assert execution.model_result.ok and execution.model_result.data['content_truncated']
    assert len(execution.model_result.model_dump_json()) <= settings.TOOL_RESULT_MAX_CHARS
    assert len(execution.raw_result.data['content']) > settings.TOOL_RESULT_MAX_CHARS
    assistant = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'long', 'type': 'function',
        'function': {'name': 'load_skill', 'arguments': '{"name":"text-report"}'}}]}
    current = [{'role': 'user', 'content': '统计'}, assistant, execution.model_message()]
    snapshot = freeze_config(settings.model_copy(update={'CHAT_CONTEXT_WINDOW': 5000,
        'CHAT_MAX_OUTPUT_TOKENS': 500, 'CONTEXT_SAFETY_TOKENS': 100}), [])
    controller = ContextController(snapshot, [], run.emit)
    messages, estimate = await controller.prepare(None, '系统权限', current, [])
    assert estimate.tokens <= snapshot.budget and protocol_complete(messages)
    assert TokenEstimator(None).estimate(messages, []).tokens <= snapshot.budget
    assert messages[0]['role'] == 'system' and '长正文' not in messages[0]['content']


def test_metadata_http_auth_and_no_body(catalog):
    app = FastAPI()
    app.include_router(router)
    app.state.skills = catalog
    def denied():
        raise HTTPException(401)
    app.dependency_overrides[current_user] = denied
    client = TestClient(app)
    assert client.get('/api/skills').status_code == 401
    app.dependency_overrides[current_user] = lambda: str(uuid4())
    response = client.get('/api/skills')
    assert response.status_code == 200
    assert response.json() == [item.model_dump(mode='json') for item in catalog.statuses()]
    assert '技能正文标记' not in response.text and '参考正文标记' not in response.text


def run_script(input_path, output):
    return subprocess.run([sys.executable, str(ROOT / 'skills/text-report/scripts/report.py'),
        '--input', str(input_path), '--output-dir', str(output)], capture_output=True, text=True)


def test_real_cli_report_csv_content_and_repeatability(tmp_path):
    documents = [
        {'document_id': 'd1', 'original_name': '非正文,名字\n含|符号.md',
         'content': '中文 中文，测试。\nHello hello WORLD\n\n中文\n\n\nworld cats'},
        {'document_id': 'd2', 'original_name': '二.md', 'content': '测试 HELLO\r\n \u3000\t\r\nworld'},
    ]
    path, output = tmp_path / 'documents.txt', tmp_path / 'output'
    path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in documents), encoding='utf-8')
    result = run_script(path, output)
    assert result.returncode == 0, result.stderr
    report = (output / 'report.md').read_text()
    assert '文档数：2' in report and '总段落数：5' in report and '总词项数：12' in report
    assert '| hello | 3 |' in report and '| 中文 | 3 |' in report and '| world | 3 |' in report
    assert '含\\|符号.md' in report and '<br>' in report
    with (output / 'stats.csv').open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [
        dict(document_id='d1', original_name=documents[0]['original_name'], metric='paragraph_count', keyword='', value='3'),
        *[dict(document_id='d1', original_name=documents[0]['original_name'], metric='keyword_count', keyword=word, value=str(count))
          for word, count in [('中文', 3), ('hello', 2), ('world', 2), ('cats', 1), ('测试', 1)]],
        dict(document_id='d2', original_name='二.md', metric='paragraph_count', keyword='', value='2'),
        *[dict(document_id='d2', original_name='二.md', metric='keyword_count', keyword=word, value='1')
          for word in ['hello', 'world', '测试']],
    ]
    before = [(output / filename).read_bytes() for filename in ['report.md', 'stats.csv']]
    assert run_script(path, output).returncode == 0
    assert before == [(output / filename).read_bytes() for filename in ['report.md', 'stats.csv']]


@pytest.mark.parametrize('content', ['坏JSON', '{"document_id":"d","original_name":"a"}',
    '{"document_id":"d","original_name":"a","content":1}',
    '{"document_id":"d","original_name":"a","content":"a","path":"/etc/passwd"}'])
def test_cli_invalid_input_does_not_write_outputs(tmp_path, content):
    path, output = tmp_path / 'input', tmp_path / 'output'
    path.write_text(content)
    result = run_script(path, output)
    assert result.returncode != 0 and 'text-report 失败' in result.stderr and not output.exists()


def test_cli_empty_input(tmp_path):
    path, output = tmp_path / 'input', tmp_path / 'output'
    path.write_text(' \n\n')
    assert run_script(path, output).returncode == 0
    assert '文档数：0' in (output / 'report.md').read_text()
    assert (output / 'stats.csv').read_text().splitlines() == ['document_id,original_name,metric,keyword,value']
