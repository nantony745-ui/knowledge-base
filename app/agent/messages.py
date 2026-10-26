import json
import re
from typing import Any

from app.adapters.common import AdapterError, ToolCall


class PublicText:
    def __init__(self):
        self.pending = ''
        self.hidden = False

    def feed(self, text: str) -> str:
        self.pending += text
        output = []
        while self.pending:
            marker = '</think>' if self.hidden else '<think>'
            position = self.pending.lower().find(marker)
            if position >= 0:
                if not self.hidden:
                    output.append(self.pending[:position])
                self.pending = self.pending[position + len(marker):]
                self.hidden = not self.hidden
                continue
            keep = next((size for size in range(len(marker) - 1, 0, -1)
                         if self.pending.lower().endswith(marker[:size])), 0)
            ready = self.pending[:-keep] if keep else self.pending
            if not self.hidden:
                output.append(ready)
            self.pending = self.pending[-keep:] if keep else ''
            break
        return ''.join(output)

    def finish(self) -> str:
        # 未闭合的思维块和开标签片段不进入公开正文。
        self.pending = ''
        return ''


def assistant_calls(content: str, calls: list[ToolCall], used_ids: set[str]) -> dict[str, Any]:
    ids = [call.id for call in calls]
    if (len(set(ids)) != len(ids) or used_ids.intersection(ids)
            or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item) for item in ids)):
        raise AdapterError('invalid_tool_calls', False, '工具调用ID缺失、重复或无效', None)
    return {'role': 'assistant', 'content': content or None, 'tool_calls': [
        {'id': call.id, 'type': 'function', 'function': {
            'name': call.name, 'arguments': call.raw_arguments}} for call in calls]}


def tool_envelope(raw_result: dict, model_result: dict) -> str:
    return json.dumps({'raw_result': raw_result, 'model_result': model_result},
                      ensure_ascii=False, allow_nan=False)


def interrupted_envelope(code: str) -> str:
    result = {'ok': False, 'data': None, 'sources': [], 'raw_result_ref': None,
              'error': {'code': code, 'message': '工具调用已取消' if code == 'cancelled'
                        else '工具调用因运行中断未取得结果', 'retryable': False}}
    return tool_envelope(result, result)


def model_message(message: dict) -> dict:
    result = {'role': message['role'], 'content': message['content']}
    if message.get('tool_calls'):
        result['tool_calls'] = message['tool_calls']
    if message['role'] == 'tool':
        payload = json.loads(message['content'])
        result['content'] = json.dumps(payload['model_result'], ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        result['tool_call_id'] = message['tool_call_id']
    return result


def protocol_complete(messages: list[dict]) -> bool:
    pending = []
    used = set()
    for message in messages:
        if message['role'] == 'system':
            continue
        if pending:
            if message['role'] != 'tool' or message.get('tool_call_id') != pending.pop(0):
                return False
        elif message['role'] == 'tool':
            return False
        calls = message.get('tool_calls') or []
        if calls:
            ids = [call['id'] for call in calls]
            if message['role'] != 'assistant' or len(set(ids)) != len(ids) or used.intersection(ids):
                return False
            pending.extend(ids)
            used.update(ids)
    return not pending


def history_window(messages: list[dict], turns: int) -> list[dict]:
    groups = {}
    for message in messages:
        if message['role'] != 'system':
            groups.setdefault(message['turn_id'], []).append(message)
    eligible = []
    for group in groups.values():
        complete = [m for m in group if m['status'] == 'complete']
        has_calls = any(m.get('tool_calls') for m in complete)
        successful = (len(complete) == len(group) and len(group) >= 2
                      and group[-1]['role'] == 'assistant' and not group[-1].get('tool_calls'))
        if (complete and complete[0]['role'] == 'user' and (successful or has_calls)
                and protocol_complete(complete)):
            eligible.append(complete)
    return [model_message(m) for group in eligible[-turns:] for m in group]


def tool_summary(content: str) -> str:
    result = json.loads(content)['model_result']
    if not result['ok']:
        return result['error']['message'][:200]
    data = result['data'] or {}
    for key, label in (('documents', '份文档'), ('hits', '条证据'), ('chunks', '个片段')):
        if key in data:
            return f"返回 {len(data[key])} {label}"
    return '工具执行完成'
