import copy
import json
from typing import Any


class ModelResultTooLarge(ValueError):
    """即使正文缩至空串，导航字段仍无法放入模型预算。"""


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def shorten_fields(value: Any, limit: int) -> Any:
    if isinstance(value, list):
        return [shorten_fields(item, limit) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {'content', 'original_name', 'title', 'document_title', 'kb_name', 'section',
                   'message', 'text', 'stdout', 'stderr'} and isinstance(item, str) and len(item) > limit:
            result[key] = item[:limit]
            if key != 'message':
                result[f'{key}_truncated'] = True
        else:
            result[key] = shorten_fields(item, limit)
    return result


def truncate_result(value: dict, max_chars: int) -> dict:
    data = copy.deepcopy(value)
    if len(json_text(data)) <= max_chars:
        return data
    if isinstance(data.get('data'), dict):
        data['data']['truncated'] = True
    best = shorten_fields(data, 0)
    if len(json_text(best)) > max_chars:
        raise ModelResultTooLarge('工具结果的必需字段超过模型长度上限')
    low, high = 0, max_chars
    while low <= high:
        middle = (low + high) // 2
        trial = shorten_fields(data, middle)
        if len(json_text(trial)) <= max_chars:
            best, low = trial, middle + 1
        else:
            high = middle - 1
    return best
