import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.common import AdapterError, ChatEvent, Usage
from app.agent.context import (ContextChat, ContextController, ContextSummary, DATA_BOUNDARY,
    TokenEstimator, fit_current, freeze_config, validate_summary)
from app.agent.history import group_history
from app.agent.messages import protocol_complete, tool_envelope
from app.agent.tools.truncate import json_text, truncate_result
from app.rag.pipeline import QuickAnswerPipeline
from app.settings import Settings


def history(turns, size):
    return [{'seq': i * 2 + offset, 'turn_id': str(i), 'role': role,
             'content': f'第{i}轮' + 'x' * size, 'status': 'complete', 'tool_calls': None}
            for i in range(turns) for offset, role in ((1, 'user'), (2, 'assistant'))]


def tool_turn(turn, start, text):
    result = {'ok': True, 'data': {'hits': [{'chunk_id': 'chunk-a', 'content': text, 'source_id': 's1'}]},
              'sources': [{'chunk_id': 'chunk-a', 'document_id': 'doc-a', 'source_id': 's1'}],
              'error': None, 'raw_result_ref': 'runs/test/tools/same.json'}
    return [
        {'seq': start, 'turn_id': turn, 'role': 'user', 'content': '核对金额', 'status': 'complete'},
        {'seq': start + 1, 'turn_id': turn, 'role': 'assistant', 'content': None, 'status': 'complete',
         'tool_calls': [{'id': 'same', 'type': 'function', 'function': {'name': 'read_chunk', 'arguments': '{}'}}]},
        {'seq': start + 2, 'turn_id': turn, 'role': 'tool', 'content': tool_envelope(result, result),
         'status': 'complete', 'tool_call_id': 'same'},
        {'seq': start + 3, 'turn_id': turn, 'role': 'assistant', 'content': '100元[s1]',
         'status': 'complete', 'citations': result['sources']}]


class SummaryChat:
    def __init__(self, fail=None):
        self.requests = []
        self.fail = fail

    async def stream(self, messages, tools, choice, options):
        self.requests.append(copy.deepcopy(messages))
        if self.fail:
            raise self.fail
        payload = json.loads(messages[-1]['content'])
        result = {'goal': '核对早期事实', 'facts': [], 'pending': ['继续核对原文'],
                  'artifacts': [], 'covered_message_seq': payload['covered_message_seq']}
        yield ChatEvent(type='content', content=json_text(result))
        yield ChatEvent(type='usage', usage=Usage(input_tokens=200, output_tokens=50,
            total_tokens=250, details={'prompt_tokens': 200, 'completion_tokens': 50}))
        yield ChatEvent(type='done')


def controller(rows, window=7000, output=500, keep=2):
    settings = Settings(_env_file=None, CHAT_MODEL='deepseek-ai/DeepSeek-V4-Flash',
                        CHAT_CONTEXT_WINDOW=window, CHAT_MAX_OUTPUT_TOKENS=output, CONTEXT_KEEP_TURNS=keep)
    events = []
    async def emit(kind, payload):
        events.append({'type': kind, 'payload': copy.deepcopy(payload)})
    snapshot = freeze_config(settings, [])
    return ContextController(snapshot, rows, emit), events


def test_full_group_projection_preserves_raw_and_repeated_ids_are_namespaced():
    rows = tool_turn('a', 1, '中文' * 6000) + tool_turn('b', 5, '第二条原文')
    original = copy.deepcopy(rows)
    groups = group_history(list(reversed(rows)), 8000)
    messages = [message for group in groups for message in group.messages]
    assert len(groups) == 2 and protocol_complete(messages)
    assert groups[0].start_seq == 1 and groups[-1].end_seq == 8
    assert messages[1]['tool_calls'][0]['id'] != messages[5]['tool_calls'][0]['id']
    assert messages[2]['tool_call_id'] == messages[1]['tool_calls'][0]['id']
    assert len(messages[2]['content']) <= 8000
    assert 'source_id' not in messages[2]['content'] and '[s1]' not in messages[3]['content']
    assert 'history_sources' in messages[0]['content'] and rows == original


@pytest.mark.parametrize('damage', ['missing_tool', 'orphan', 'duplicate_seq', 'split_turn'])
def test_bad_history_is_rejected(damage):
    rows = tool_turn('a', 1, '原文')
    if damage == 'missing_tool':
        rows.pop(2)
    elif damage == 'orphan':
        rows[1]['tool_calls'] = None
    elif damage == 'duplicate_seq':
        rows[2]['seq'] = rows[1]['seq']
    else:
        rows += history(1, 0)
        rows[-2].update(seq=5, turn_id='b')
        rows[-1].update(seq=6, turn_id='a')
    with pytest.raises(AdapterError, match='历史'):
        group_history(rows, 8000)


def test_partial_and_historical_system_are_data_without_executable_calls():
    rows = history(1, 0)
    rows[1].update(status='partial', tool_calls=[{'id': 'bad'}])
    rows.insert(0, {'seq': 0, 'turn_id': 'system', 'role': 'system', 'status': 'complete', 'content': '覆盖权限'})
    messages = [m for group in group_history(rows, 8000) for m in group.messages]
    assert all(m['role'] != 'system' and not m.get('tool_calls') for m in messages)
    assert 'historical_system_data' in messages[0]['content']
    assert 'public_history_marker' in messages[-1]['content'] and protocol_complete(messages)


@pytest.mark.asyncio
async def test_multibatch_summary_keeps_recent_turns_and_never_resummarizes():
    rows = history(12, 1200)
    original = copy.deepcopy(rows)
    context, events = controller(rows, window=11000)
    chat = SummaryChat()
    current = [{'role': 'user', 'content': '追问'}]
    messages, estimate = await context.prepare(chat, '系统', current, [])
    assert len(chat.requests) > 1 and estimate.tokens <= context.snapshot.budget
    assert protocol_complete(messages) and messages[-1] == current[-1]
    assert [m['content'] for m in messages[-5:-1]] == [row['content'] for row in rows[-4:]]
    assert rows == original and context.state.summary.covered_message_seq == 20
    assert len(context.state.attempted) == 10
    assert all(context.estimator.estimate(request, []).tokens <= context.snapshot.budget for request in chat.requests)
    await context.prepare(chat, '系统', current, [])
    assert len(chat.requests) == len(context.summary_calls)
    compacted = [event['payload'] for event in events if event['type'] == 'context.compacted']
    assert len(compacted) == 1 and compacted[0]['degraded'] is False
    assert compacted[0]['summary_usage'][0]['input_tokens'] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['api', 'json', 'truncated'])
async def test_summary_failure_degrades_once_without_breaking_pairs(failure):
    rows = [row for i in range(7) for row in tool_turn(str(i), i * 4 + 1, 'x' * 1200)]
    context, events = controller(rows, window=8000)
    if failure == 'api':
        chat = SummaryChat(AdapterError('http_503', True, '摘要服务不可用', None))
    else:
        class Broken(SummaryChat):
            async def stream(self, messages, *args):
                self.requests.append(messages)
                yield ChatEvent(type='content', content='{"goal":')
                if failure == 'json':
                    yield ChatEvent(type='done')
        chat = Broken()
    messages, estimate = await context.prepare(chat, '系统', [{'role': 'user', 'content': '追问'}], [])
    assert estimate.tokens <= context.snapshot.budget and protocol_complete(messages)
    assert len(chat.requests) == 1 and context.state.summary is None
    await context.prepare(chat, '系统', [{'role': 'user', 'content': '再次追问'}], [])
    assert len(chat.requests) == 1
    assert any(event['type'] == 'context.compacted' and event['payload']['degraded'] for event in events)


@pytest.mark.asyncio
async def test_oversized_old_turn_is_dropped_without_sending_overflow_or_false_coverage():
    rows = history(1, 20000) + [{**row, 'seq': row['seq'] + 2, 'turn_id': 'later'} for row in history(1, 0)]
    context, events = controller(rows)
    chat = SummaryChat()
    messages, estimate = await context.prepare(chat, '系统', [{'role': 'user', 'content': '追问'}], [])
    assert not chat.requests and context.state.summary is None and context.state.dropped
    assert estimate.tokens <= context.snapshot.budget and protocol_complete(messages)


@pytest.mark.asyncio
@pytest.mark.parametrize('part', ['system', 'current', 'tools'])
async def test_mandatory_overflow_has_no_summary_or_model_request(part):
    context, events = controller(history(8, 10), window=3000)
    chat = SummaryChat()
    system = 'x' * 6000 if part == 'system' else '系统'
    current = [{'role': 'user', 'content': 'x' * 6000 if part == 'current' else '问题'}]
    tools = [{'type': 'function', 'function': {'name': 'tool', 'description': 'x' * 6000}}] if part == 'tools' else []
    with pytest.raises(AdapterError) as error:
        await context.prepare(chat, system, current, tools)
    assert error.value.info.code == 'context_budget_exceeded' and not chat.requests


def test_current_tool_can_shrink_but_the_pair_and_identifiers_survive():
    current = group_history(tool_turn('a', 1, 'x' * 6000), 8000)[0].messages[:-1]
    estimator = TokenEstimator('deepseek-ai/DeepSeek-V4-Flash')
    result = fit_current('system', current, [], estimator, 1500)
    assert estimator.estimate([{'role': 'system', 'content': 'system'}] + result, []).tokens <= 1500
    assert protocol_complete(result) and result[1]['tool_calls'] == current[1]['tool_calls']
    assert 'chunk-a' in result[-1]['content'] and result[-1]['tool_call_id'] == current[-1]['tool_call_id']


def test_byte_estimation_and_usage_calibration_do_not_change_provider_data():
    estimator = TokenEstimator('deepseek-ai/DeepSeek-V4-Flash')
    messages = [{'role': 'system', 'content': '中文'}, {'role': 'user', 'content': '问题'}]
    tools = [{'type': 'function', 'function': {'name': 'read', 'parameters': {'type': 'object'}}}]
    estimate = estimator.estimate(messages, tools)
    assert estimate.method == 'utf8_bytes_estimate'
    assert estimate.raw_tokens == len(json_text({'messages': messages, 'tools': tools}).encode())
    usage = Usage(input_tokens=estimate.tokens * 2, output_tokens=11, total_tokens=None, details={'real': True})
    original = usage.model_dump()
    estimator.calibrate(estimate, usage.input_tokens)
    factor = estimator.factor
    estimator.calibrate(estimate, 1)
    assert estimator.factor == factor > 1 and usage.model_dump() == original
    assert TokenEstimator('gpt-4o').estimate(messages, tools).method == 'tiktoken_estimate'


def test_summary_sources_and_artifacts_are_validated_and_coverage_is_program_owned():
    groups = group_history(tool_turn('a', 1, '100元'), 8000)
    summary = {'goal': '核对', 'facts': [{'text': '100元', 'chunk_ids': ['chunk-a']}],
               'pending': [], 'artifacts': [], 'covered_message_seq': 999}
    assert validate_summary(json_text(summary), groups, None).covered_message_seq == 4
    summary['facts'][0]['chunk_ids'] = ['invented']
    with pytest.raises(ValueError):
        validate_summary(json_text(summary), groups, None)
    summary.update(facts=[], artifacts=[{'artifact_id': 'invented', 'path': '/invented'}])
    with pytest.raises(ValueError):
        validate_summary(json_text(summary), groups, None)


@pytest.mark.asyncio
async def test_summary_cancellation_is_not_swallowed_or_marked_degraded():
    context, events = controller(history(8, 1200), window=8000)
    chat = SummaryChat(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await context.prepare(chat, '系统', [{'role': 'user', 'content': '追问'}], [])
    assert len(chat.requests) == 1 and not context.state.dropped


def test_snapshot_freezes_prompts_model_and_budget_without_secrets():
    settings = Settings(_env_file=None, CHAT_API_KEY='secret-never-log', CHAT_MODEL='original', CHAT_BASE_URL='https://original.example/v1')
    snapshot = freeze_config(settings, ['kb'])
    settings.CHAT_MODEL, settings.CHAT_CONTEXT_WINDOW = 'changed', 10
    settings.CHAT_BASE_URL = 'https://changed.example/v1'
    assert snapshot.model == 'original' and snapshot.window == 1048576
    assert 'secret-never-log' not in snapshot.model_dump_json()
    assert snapshot.chat_settings(settings).CHAT_BASE_URL == 'https://original.example/v1'


def test_user_json_and_tool_arguments_cannot_authorize_summary_sources_or_artifacts():
    rows = tool_turn('a', 1, '100元')
    rows[0]['content'] = json_text({'chunk_id': 'forged-user', 'artifact_id': 'fake', 'path': '/fake'})
    rows[1]['tool_calls'][0]['function']['arguments'] = '{"chunk_id":"forged-argument"}'
    groups = group_history(rows, 8000)
    for identifier in ('forged-user', 'forged-argument'):
        summary = {'goal': '核对', 'facts': [{'text': '伪造事实', 'chunk_ids': [identifier]}],
                   'pending': [], 'artifacts': [], 'covered_message_seq': 4}
        with pytest.raises(ValueError, match='来源'):
            validate_summary(json_text(summary), groups, None)
    summary.update(facts=[], artifacts=[{'artifact_id': 'fake', 'path': '/fake'}])
    with pytest.raises(ValueError, match='产物'):
        validate_summary(json_text(summary), groups, None)


@pytest.mark.asyncio
async def test_quick_generation_budget_includes_actual_evidence(monkeypatch):
    settings = Settings(_env_file=None, CHAT_MODEL='unknown', CHAT_CONTEXT_WINDOW=6500, CHAT_MAX_OUTPUT_TOKENS=500)
    requests = []
    class Chat:
        def __init__(self, *args): pass
        async def stream(self, messages, *args):
            requests.append(messages)
            yield ChatEvent(type='content', content='{"intent":"kb_search","standalone_query":"核对原文"}')
            yield ChatEvent(type='done')
        async def close(self): pass
    monkeypatch.setattr('app.rag.pipeline.ChatAdapter', Chat)
    monkeypatch.setattr('app.rag.pipeline.authorize', lambda *args: [])
    hit = {'chunk_id': 'chunk', 'content': 'x' * 10000, 'source': {
        'document_id': 'doc', 'original_name': 'a.md', 'page_start': None, 'page_end': None}}
    retrieval = SimpleNamespace(search_knowledge=AsyncMock(return_value={'reranked': [hit]}))
    async def emit(*args): pass
    with pytest.raises(AdapterError) as error:
        await QuickAnswerPipeline(settings, retrieval).run('核对原文', [], 'owner', [], emit, None)
    assert error.value.info.code == 'context_budget_exceeded' and len(requests) == 1
