from uuid import uuid4

import pytest


@pytest.fixture
def source_store(monkeypatch):
    """离线运行测试的来源仓储替身；集成测试不使用此夹具。"""
    import copy
    from app.db import source_repository
    storage = {}
    def register(run_id, owner, kb_ids, items):
        rows = storage.setdefault(run_id, [])
        output = []
        for item in items:
            row = next((row for row in rows if row['chunk_id'] == item['chunk_id']), None)
            if row is None:
                row = {'run_id': run_id, 'handle': f's{len(rows) + 1}', 'chunk_id': item['chunk_id'],
                    'document_id': str(uuid4()), 'evidence_granted': False, 'readable': True,
                    'source_snapshot': {'kb_id': next(iter(kb_ids), str(uuid4())),
                        'original_name': item.get('content', '') + '.md', 'page_start': None,
                        'page_end': None, 'excerpt': ''}}
                rows.append(row)
            output.append(copy.deepcopy(row))
        return output
    def grant(run_id, owner, kb_ids, fragments):
        for handle, chunk_id, excerpt in fragments:
            row = next((row for row in storage.get(run_id, []) if row['handle'] == handle and
                        row['chunk_id'] == chunk_id), None)
            if row is not None and excerpt.strip() and row['readable']:
                row['evidence_granted'] = True
                row['source_snapshot']['excerpt'] = excerpt
    monkeypatch.setattr(source_repository, 'register_sources', register)
    monkeypatch.setattr(source_repository, 'grant_sources', grant)
    monkeypatch.setattr(source_repository, 'current_sources', lambda rid, *_: copy.deepcopy(storage.get(rid, [])))
    monkeypatch.setattr('app.services.runs.register_run', lambda *args: None)
    return storage
