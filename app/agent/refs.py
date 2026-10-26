from typing import Any

from app.adapters.common import AdapterError


class RunRefs:
    def __init__(self):
        self.refs: dict[str, str] = {}
        self.reverse: dict[tuple[str, str], str] = {}
        self.numbers = {'d': 0, 'c': 0}

    def register(self, kind: str, identifier: str) -> str:
        key = (kind, identifier)
        if key not in self.reverse:
            self.numbers[kind] += 1
            handle = f'{kind}{self.numbers[kind]}'
            self.reverse[key] = handle
            self.refs[handle] = identifier
        return self.reverse[key]

    def resolve(self, handle: str, kind: str) -> str:
        if not handle.startswith(kind) or handle not in self.refs:
            raise AdapterError('reference_unavailable', False, '本轮资源句柄不存在，请重新搜索或查找文档', None)
        return self.refs[handle]

    def project(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.project(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {}
        for key, item in value.items():
            kind = {'document_id': 'd', 'chunk_id': 'c', 'target_chunk_id': 'c'}.get(key)
            if kind:
                result[key.replace('_id', '_ref')] = self.register(kind, item) if item else None
            elif key == 'chunk_ids':
                result['chunk_refs'] = [self.register('c', identifier) for identifier in item]
            else:
                result[key] = self.project(item)
        return result

    def restore_summary(self, value: dict) -> dict:
        return {**value, 'facts': [
            {**{key: item for key, item in fact.items() if key != 'chunk_refs'},
             'chunk_ids': [self.resolve(handle, 'c') for handle in fact['chunk_refs']]}
            for fact in value['facts']]}
