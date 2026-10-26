import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.adapters.chat import ToolBuffer
from app.adapters.common import AdapterError, ChatEvent, ToolCall
from app.agent.engine import ReasoningEngine
from app.agent.messages import PublicText, history_window, interrupted_envelope, protocol_complete
from app.agent.tools import ToolDefinition, ToolRegistry, ToolResult
from app.agent.tools.base import ToolContext
from app.agent.tools.knowledge import ReadChunkArgs, SearchKnowledgeArgs
from app.api.auth import current_user, require_origin
from app.api.runs import router
from app.services.runs import Run, RunManager
from app.settings import Settings


def call(identifier, arguments='{"query":"部署"}', name='search_knowledge'):
    return ChatEvent(type='tool_call', tool_call=ToolCall(
        id=identifier, name=name, arguments=None, raw_arguments=arguments))


def response(*events):
    return [*events, ChatEvent(type='done')]


class FakeChat:
    def __init__(self, rounds):
        self.rounds = rounds
        self.requests = []
        self.request_tools = []
        self.closed = False

    async def stream(self, messages, tools, choice, options, *, defer_tool_argument_validation):
        assert defer_tool_argument_validation and choice == ('auto' if tools else None)
        assert protocol_complete(messages)
        self.requests.append(copy.deepcopy(messages))
        self.request_tools.append(copy.deepcopy(tools))
        events = self.rounds[len(self.requests) - 1]
        for event in events:
            if isinstance(event, BaseException):
                raise event
            yield event

    async def close(self):
        self.closed = True


@pytest.fixture
def runtime(monkeypatch, tmp_path, source_store):
    monkeypatch.setattr('app.db.mcp_repository.list_services', lambda owner: [])
    settings = Settings(_env_file=None, DATA_DIR=tmp_path, LANGFUSE_BASE_URL=None,
                        LANGFUSE_PUBLIC_KEY=None, LANGFUSE_SECRET_KEY=None)
    run = Run(str(uuid4()), str(uuid4()), str(uuid4()), turn_id=str(uuid4()), mode='smart_reasoning')
    rows = [{'id': run.turn_id, 'turn_id': run.turn_id, 'seq': 1, 'role': 'user',
             'content': '比较两文档', 'status': 'complete', 'tool_calls': None}]

    def append(sid, owner, role, content, turn, status, citations, run_id, calls, call_id):
        row = {'id': str(uuid4()), 'seq': len(rows) + 1, 'turn_id': turn, 'role': role,
               'content': content, 'status': status, 'citations': citations,
               'run_id': run_id, 'tool_calls': calls, 'tool_call_id': call_id}
        rows.append(row)
        return row

    def repair(sid, owner, turn, code):
        saved = []
        existing = {row.get('tool_call_id') for row in rows if row['role'] == 'tool'}
        for row in list(rows):
            for item in row.get('tool_calls') or []:
                if item['id'] not in existing:
                    saved.append(append(sid, owner, 'tool', interrupted_envelope(code), turn,
                                        'complete', None, run.id, None, item['id']))
                    existing.add(item['id'])
        return saved

    async def context(owner, sid, rid, kb_ids, allowed, cancel, refs):
        return ToolContext(owner, sid, rid, frozenset(kb_ids), frozenset(allowed), cancel, refs)

    monkeypatch.setattr('app.agent.engine.append_run_message', append)
    monkeypatch.setattr('app.services.runs.append_run_message', append)
    monkeypatch.setattr('app.services.runs.repair_tool_results', repair)
    monkeypatch.setattr('app.agent.engine.create_tool_context', context)
    monkeypatch.setattr('app.agent.engine.knowledge_scope', lambda context: [])
    handler = AsyncMock(return_value=ToolResult(ok=True, data={'hits': []}))
    registry = ToolRegistry(settings, lambda name, result: result)
    registry.register(ToolDefinition('search_knowledge', '搜索', SearchKnowledgeArgs, True, handler))
    engine = ReasoningEngine(settings, registry)
    manager = RunManager(settings, None, None, engine)
    return SimpleNamespace(settings=settings, run=run, rows=rows, handler=handler,
                           registry=registry, engine=engine, manager=manager)


def install_chat(monkeypatch, rounds):
    chat = FakeChat(rounds)
    monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat)
    return chat


def hit(text):
    cid = str(uuid4())
    return {'chunk_id': cid, 'content': text}, {'chunk_id': cid, 'document_id': str(uuid4()),
        'kb_id': str(uuid4()), 'original_name': text + '.md', 'page_start': None, 'page_end': None}


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['model', 'tool', 'final'])
async def test_user_stop_preserves_public_text_and_next_reasoning_context(runtime, monkeypatch, phase):
    entered = asyncio.Event()
    if phase in {'model', 'final'}:
        class SlowChat(FakeChat):
            async def stream(self, messages, *args, **kwargs):
                if phase == 'final' and len(self.requests) < 10:
                    async for event in super().stream(messages, *args, **kwargs):
                        yield event
                    return
                if phase == 'final':
                    assert args[0] is None
                self.requests.append(copy.deepcopy(messages))
                yield ChatEvent(type='content', content='第一点：已经核对。')
                entered.set()
                await asyncio.Event().wait()
        chat = SlowChat([response(call(f'call_{i}')) for i in range(10)] if phase == 'final' else [])
        monkeypatch.setattr('app.agent.engine.ChatAdapter', lambda *args: chat)
    else:
        chat = install_chat(monkeypatch, [response(
            ChatEvent(type='content', content='第一点：已经核对。'), call('call_a'), call('call_b'))])
        async def wait(*args):
            entered.set()
            await asyncio.Event().wait()
        runtime.handler.side_effect = wait
    run, manager = runtime.run, runtime.manager
    manager.runs[run.id] = run
    manager.active[run.session_id] = run.id
    run.task = asyncio.create_task(manager.execute(run, '说明步骤', [], [], run.turn_id))
    await asyncio.wait_for(entered.wait(), 5)
    await manager.stop(run.id, run.owner)
    assert chat.closed and not manager.active
    assert any(row['content'] == '第一点：已经核对。' for row in runtime.rows)
    if phase in {'model', 'final'}:
        assert runtime.rows[-1]['status'] == 'partial'
        assert runtime.handler.await_count == (10 if phase == 'final' else 0)
    else:
        tools = [row for row in runtime.rows if row['role'] == 'tool']
        assert {row['tool_call_id'] for row in tools} == {'call_a', 'call_b'}
        assert all(json.loads(row['content'])['model_result']['error']['code'] == 'cancelled' for row in tools)
        assert len([row for row in runtime.rows if row['content'] == '第一点：已经核对。']) == 1
    history = copy.deepcopy(runtime.rows)
    next_run = Run(str(uuid4()), run.session_id, run.owner, turn_id=str(uuid4()), mode='smart_reasoning')
    runtime.rows.append({'id': next_run.turn_id, 'seq': len(runtime.rows) + 1, 'turn_id': next_run.turn_id,
                         'role': 'user', 'content': '继续', 'status': 'complete', 'tool_calls': None})
    next_chat = install_chat(monkeypatch, [response(ChatEvent(type='content', content='第二点：继续说明。'))])
    await manager.execute(next_run, '继续', [], history, next_run.turn_id)
    assert next_run.events[-1]['type'] == 'run.completed'
    assert any('第一点：已经核对。' in (message['content'] or '') for message in next_chat.requests[0])
    assert protocol_complete(next_chat.requests[0])


def test_interleaved_tool_deltas_preserve_raw_invalid_json_and_order():
    buffer = ToolBuffer()
    delta = lambda index, ident, name, args: SimpleNamespace(index=index, id=ident,
        function=SimpleNamespace(name=name, arguments=args))
    buffer.append([delta(1, 'call_b', 'search_', '{"query":'), delta(0, 'call_a', 'search_', '{"query":')])
    buffer.append([delta(0, None, 'knowledge', '"甲"}'), delta(1, None, 'knowledge', '')])
    with pytest.raises(AdapterError):
        buffer.complete('length', True)
    calls = buffer.complete('tool_calls', True)
    assert [item.id for item in calls] == ['call_a', 'call_b']
    assert calls[0].raw_arguments == '{"query":"甲"}'
    assert calls[1].raw_arguments == '{"query":'
    assert all(item.arguments is None for item in calls)


@pytest.mark.parametrize('chunks,expected', [
    (['公开<th', 'ink>私人', '内容</th', 'ink>回答'], '公开回答'),
    (['<think>未闭合秘密'], ''), (['甲', '乙'], '甲乙'),
    (['<THINK>秘密</THINK>公开'], '公开'),
])
def test_private_blocks_never_reach_public_stream(chunks, expected):
    public = PublicText()
    assert ''.join(public.feed(item) for item in chunks) + public.finish() == expected


@pytest.mark.asyncio
async def test_multi_tool_assistant_saved_once_and_ordered_results_consumed(runtime, monkeypatch):
    first, source1 = hit('采购条款')
    second, source2 = hit('安全条款')
    runtime.handler.side_effect = [ToolResult(ok=True, data={'hits': [first]}, sources=[source1]),
                                   ToolResult(ok=True, data={'hits': [second]}, sources=[source2])]
    chat = install_chat(monkeypatch, [response(ChatEvent(type='content', content='<think>秘密</think>核对资料'),
        call('call_a'), call('call_b')), response(ChatEvent(type='content', content='采购[s1]，安全[s2]。'))])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    rows = runtime.rows
    assert [row['role'] for row in rows] == ['user', 'assistant', 'tool', 'tool', 'assistant']
    assert [item['id'] for item in rows[1]['tool_calls']] == ['call_a', 'call_b']
    assert [row['tool_call_id'] for row in rows[2:4]] == ['call_a', 'call_b']
    assert protocol_complete(chat.requests[1]) and chat.requests[1][-1]['role'] == 'tool'
    assert all(row['turn_id'] == runtime.run.turn_id for row in rows)
    assert json.loads(rows[2]['content'])['raw_result']['data']['hits'][0] == first
    assert json.loads(rows[2]['content'])['model_result']['data']['hits'][0]['source_id'] == 's1'
    assert rows[-1]['content'] == '采购[s1]，安全[s2]。' and len(rows[-1]['citations']) == 2
    assert runtime.run.events[-1]['type'] == 'run.completed'
    assert '秘密' not in json.dumps(runtime.run.events, ensure_ascii=False)
    assert '秘密' not in json.dumps(rows, ensure_ascii=False)
    assert runtime.handler.await_count == 2 and chat.closed


@pytest.mark.asyncio
async def test_bad_json_returns_tool_error_then_model_corrects(runtime, monkeypatch):
    chat = install_chat(monkeypatch, [response(call('bad', '{"query":')),
        response(call('corrected')), response(ChatEvent(type='content', content='已完成'))])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    assert runtime.handler.await_count == 1
    bad = json.loads(runtime.rows[2]['content'])
    assert bad['model_result']['error']['code'] == 'invalid_json'
    assert json.loads(chat.requests[1][-1]['content'])['error']['code'] == 'invalid_json'
    assert runtime.run.events[-1]['type'] == 'run.completed'


@pytest.mark.asyncio
async def test_search_then_read_chunk_consumes_context_and_reuses_target_citation(runtime, monkeypatch):
    target, source = hit('任何SaaS采购均需安全评估。')
    neighbor, neighbor_source = hit('涉及客户数据时还需法务联合评估。')
    neighbor_source.update({key: source[key] for key in ('document_id', 'kb_id', 'original_name')})
    runtime.handler.return_value = ToolResult(ok=True, data={'hits': [target]}, sources=[source])
    reader = AsyncMock(return_value=ToolResult(ok=True, data={
        'document_id': source['document_id'], 'target_chunk_id': target['chunk_id'],
        'chunks': [{**target, 'ordinal': 3}, {**neighbor, 'ordinal': 4}]}, sources=[source, neighbor_source]))
    runtime.registry.register(ToolDefinition('read_chunk', '读取邻文', ReadChunkArgs, True, reader))
    args = json.dumps({'chunk_ref': 'c1', 'before': 0, 'after': 1})
    chat = install_chat(monkeypatch, [response(call('search')),
        response(call('context', args, 'read_chunk')),
        response(ChatEvent(type='content', content='均需安全评估[s1]；涉及客户数据还需联合评估[s2]。'))])
    await runtime.manager.execute(runtime.run, '请核对条件与例外', [], [], runtime.run.turn_id)
    assert {schema['function']['name'] for schema in runtime.registry.schemas(reader.await_args.args[1])} == {'search_knowledge', 'read_chunk'}
    assert reader.await_args.args[0] == ReadChunkArgs.model_validate_json(args)
    observed = json.loads(chat.requests[2][-1]['content'])
    assert observed['data']['target_chunk_ref'] == 'c1'
    assert [item['source_id'] for item in observed['data']['chunks']] == ['s1', 's2']
    assert [row['role'] for row in runtime.rows] == ['user', 'assistant', 'tool', 'assistant', 'tool', 'assistant']
    assert runtime.rows[3]['tool_calls'][0]['function']['name'] == 'read_chunk'
    assert runtime.rows[4]['tool_call_id'] == 'context'
    assert runtime.run.events[-1]['type'] == 'run.completed' and len(runtime.rows[-1]['citations']) == 2


@pytest.mark.asyncio
async def test_tool_error_is_observation_not_illegal_history(runtime, monkeypatch):
    runtime.handler.side_effect = RuntimeError('内部秘密')
    chat = install_chat(monkeypatch, [response(call('failure')), response(ChatEvent(type='content', content='工具失败，依据不足'))])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    result = json.loads(chat.requests[1][-1]['content'])
    assert result['error']['code'] == 'tool_execution_failed'
    assert '内部秘密' not in json.dumps(result, ensure_ascii=False)
    assert runtime.run.events[-1]['type'] == 'run.completed'


@pytest.mark.asyncio
async def test_ten_tool_rounds_generate_final_answer_from_context_without_more_tools(runtime, monkeypatch):
    first, first_source = hit('采购必须评估。')
    last, last_source = hit('安全部门负责评估。')
    runtime.handler.side_effect = [ToolResult(ok=True, data={'hits': [first]}, sources=[first_source]),
        *[ToolResult(ok=True, data={'hits': []}) for _ in range(8)],
        ToolResult(ok=True, data={'hits': [last]}, sources=[last_source])]
    answer = '采购必须评估[s1]，安全部门负责评估[s2]；其他条件依据不足。'
    chat = install_chat(monkeypatch, [
        *[response(ChatEvent(type='content', content='将查询'), call(f'call_{i}')) for i in range(10)],
        response(ChatEvent(type='content', content=answer))])
    history = [{'turn_id': 'previous', 'seq': 0, 'role': 'user', 'content': '请关注采购条件', 'status': 'complete'}]
    await runtime.manager.execute(runtime.run, '比较两文档', [], history, runtime.run.turn_id)
    assert len(chat.requests) == 11 and runtime.handler.await_count == 10
    assert all(chat.request_tools[:10]) and chat.request_tools[-1] is None
    final = chat.requests[-1]
    assert any(message['content'] == '请关注采购条件' for message in final)
    assert any('比较两文档' in (message['content'] or '') for message in final)
    results = [json.loads(message['content']) for message in final if message['role'] == 'tool']
    assert len(results) == 10
    assert results[0]['data']['hits'][0]['content'] == first['content']
    assert results[-1]['data']['hits'][0]['content'] == last['content']
    assert final[-1]['role'] == 'user' and '停止调用工具' in final[-1]['content']
    assert runtime.run.events[-1]['type'] == 'run.completed'
    assert runtime.run.events[-2]['type'] == 'answer.finalized'
    assert runtime.run.events[-2]['payload']['text'] == answer
    assert runtime.rows[-1]['status'] == 'complete' and runtime.rows[-1]['content'] == answer
    assert len(runtime.rows[-1]['citations']) == 2
    assert any(event['type'] == 'model.delta' and event['payload']['round_no'] == 11
               and event['payload']['text'] == answer for event in runtime.run.events)
    assert protocol_complete(history_window(runtime.rows, 6)) and chat.closed


@pytest.mark.asyncio
async def test_answer_on_tenth_round_does_not_request_forced_final_answer(runtime, monkeypatch):
    chat = install_chat(monkeypatch, [*[response(call(f'call_{i}')) for i in range(9)],
        response(ChatEvent(type='content', content='现有资料不足，无法确认。'))])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    assert len(chat.requests) == 10 and runtime.handler.await_count == 9
    assert all(chat.request_tools)
    assert runtime.run.events[-1]['type'] == 'run.completed' and chat.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['empty', 'truncated'])
async def test_forced_final_answer_keeps_existing_model_failure_handling(runtime, monkeypatch, failure):
    final = (response() if failure == 'empty' else
             [ChatEvent(type='content', content='部分结论'), AdapterError('incomplete_stream', False, '流中断', None)])
    chat = install_chat(monkeypatch, [*[response(call(f'call_{i}')) for i in range(10)], final])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    assert len(chat.requests) == 11 and runtime.handler.await_count == 10
    assert chat.request_tools[-1] is None
    assert runtime.run.events[-1]['type'] == 'run.failed'
    assert runtime.run.events[-1]['payload']['code'] == ('empty_answer' if failure == 'empty' else 'incomplete_stream')
    assert runtime.rows[-1]['content'] == ('' if failure == 'empty' else '部分结论')
    assert runtime.rows[-1]['status'] == ('failed' if failure == 'empty' else 'partial')
    assert protocol_complete(history_window(runtime.rows, 6)) and chat.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['truncated', 'duplicate', 'reused_id'])
async def test_invalid_complete_group_cannot_execute(runtime, monkeypatch, failure):
    if failure == 'truncated':
        rounds = [[call('partial'), AdapterError('incomplete_stream', False, '流中断', None)]]
    elif failure == 'duplicate':
        rounds = [response(call('same'), call('same'))]
    else:
        rounds = [response(call('same')), response(call('same'))]
    chat = install_chat(monkeypatch, rounds)
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    assert runtime.handler.await_count == (1 if failure == 'reused_id' else 0)
    assert runtime.run.events[-1]['type'] == 'run.failed' and chat.closed
    assert len([row for row in runtime.rows if row.get('tool_calls')]) == (1 if failure == 'reused_id' else 0)


@pytest.mark.asyncio
async def test_cancellation_fills_whole_batch_once_and_retains_legal_history(runtime, monkeypatch):
    entered = asyncio.Event()
    async def waiting(args, context):
        entered.set()
        await asyncio.Event().wait()
    runtime.handler.side_effect = waiting
    install_chat(monkeypatch, [response(call('call_a'), call('call_b'))])
    task = asyncio.create_task(runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(task, 3)
    tools = [row for row in runtime.rows if row['role'] == 'tool']
    assert [row['tool_call_id'] for row in tools] == ['call_a', 'call_b']
    assert all(json.loads(row['content'])['model_result']['error']['code'] == 'cancelled' for row in tools)
    history = history_window(runtime.rows, 6)
    assert [row['role'] for row in history] == ['user', 'assistant', 'tool', 'tool']
    assert protocol_complete(history) and runtime.run.terminal


def test_window_keeps_turn_groups_and_excludes_partial_answer():
    rows = []
    for i in range(8):
        rows += [{'turn_id': str(i), 'role': 'user', 'content': f'问题{i}', 'status': 'complete'},
                 {'turn_id': str(i), 'role': 'assistant', 'content': f'答案{i}', 'status': 'complete'}]
    rows += [{'turn_id': 'bad', 'role': 'user', 'content': '未完成', 'status': 'complete'},
             {'turn_id': 'bad', 'role': 'assistant', 'content': '半截答案', 'status': 'partial'}]
    history = history_window(rows, 6)
    assert len(history) == 12 and history[0]['content'] == '问题2'
    assert all(item['content'] != '半截答案' for item in history)


@pytest.mark.asyncio
@pytest.mark.parametrize('answer,expected', [
    ('保留模型回答正文。', '保留模型回答正文。'),
    ('保留模型回答正文[s99]。', '保留模型回答正文。'),
])
async def test_smart_answer_saved_without_citations_after_search(runtime, monkeypatch, answer, expected):
    passage, source = hit('检索到的资料')
    runtime.handler.return_value = ToolResult(ok=True, data={'hits': [passage]}, sources=[source])
    install_chat(monkeypatch, [response(call('search')),
        response(ChatEvent(type='content', content=answer))])
    await runtime.manager.execute(runtime.run, '比较两文档', [], [], runtime.run.turn_id)
    assert runtime.run.sources.registered
    assert runtime.rows[-1]['content'] == expected and runtime.rows[-1]['citations'] == []
    assert runtime.run.final_payload['text'] == expected
    assert runtime.run.events[-1]['type'] == 'run.completed'


@pytest.mark.asyncio
async def test_smart_citations_preserve_answer_and_clean_invalid_markers(source_store):
    from app.agent.sources import RunSources
    evidence = RunSources(str(uuid4()), str(uuid4()), [])
    text, citations = await evidence.validate('没有合法引用[s99]')
    assert text == '没有合法引用' and citations == []


def test_api_modes_forward_same_session_and_reject_unknown_mode():
    application = FastAPI()
    application.include_router(router)
    create = AsyncMock(return_value='run')
    application.state.runs = SimpleNamespace(create=create)
    application.dependency_overrides[current_user] = lambda: 'owner'
    application.dependency_overrides[require_origin] = lambda: None
    with TestClient(application) as client:
        for mode in ('quick_answer', 'smart_reasoning'):
            assert client.post('/api/sessions/session/runs', json={'query': '问题', 'mode': mode}).status_code == 202
            assert create.call_args.args == ('session', 'owner', '问题', [], mode)
        assert client.post('/api/sessions/session/runs', json={'query': '问题', 'mode': 'unknown'}).status_code == 422
