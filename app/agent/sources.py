import json
import logging
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.agent.refs import RunRefs
from app.agent.tools.truncate import ModelResultTooLarge
from app.db import source_repository as repository

logger = logging.getLogger(__name__)
HANDLE = re.compile(r's[1-9][0-9]{0,14}\Z')


class SourceSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    original_name: str
    page_start: int | None
    page_end: int | None
    excerpt: str
    kb_id: UUID


class SourceReference(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: UUID
    handle: str = Field(pattern=r'^s[1-9][0-9]{0,14}$')
    chunk_id: UUID | None
    document_id: UUID | None
    source_snapshot: SourceSnapshot
    evidence_granted: bool


class SourceView(SourceReference):
    original_deleted: bool
    readable: bool


def citation_card(source):
    return {'run_id': source['run_id'], 'source_id': source['handle'], 'chunk_id': source['chunk_id'],
            'document_id': source['document_id'], **source['source_snapshot']}


def parse_citations(text, sources):
    found = {source['handle']: source for source in sources}
    used, errors = [], []
    def replace(match):
        handle = match[1]
        source = found.get(handle)
        reason = ('unknown_source' if source is None else 'unread_source' if not source['evidence_granted']
                  else 'source_unavailable' if not source.get('readable') else None)
        if reason:
            errors.append({'handle': handle, 'code': reason})
            return ''
        if handle not in used:
            used.append(handle)
        return match[0]
    cleaned = re.sub(r'\[(s\d+)\]', replace, text).strip()
    citations = [citation_card(found[handle]) for handle in used]
    return cleaned, citations, errors


class RunSources:
    def __init__(self, run_id, owner, kb_ids, refs=None):
        self.run_id, self.owner, self.kb_ids = run_id, owner, list(kb_ids)
        self.refs = refs if refs is not None else RunRefs()
        self.registered = {}

    def chunk_id(self, item):
        return self.refs.resolve(item['chunk_ref'], 'c') if 'chunk_ref' in item else item['chunk_id']

    async def annotate(self, execution, max_chars):
        from app.agent.tools.base import ToolExecution, ToolResult, limit_model_result
        from app.services.runs import atomic_thread
        result = execution.model_result.model_copy(deep=True)
        if not result.ok:
            return execution
        items = (result.data or {}).get('hits', (result.data or {}).get('chunks', []))
        if not items:
            return execution
        internal = [{**item, 'chunk_id': self.chunk_id(item)} for item in items]
        rows = await atomic_thread(repository.register_sources, self.run_id, self.owner, self.kb_ids, internal)
        mapping = {row['chunk_id']: row for row in rows}
        self.registered.update({row['handle']: row for row in rows})
        for item in items:
            item['source_id'] = mapping[self.chunk_id(item)]['handle']
        result.data = self.refs.project(result.data)
        result.sources = []
        try:
            model_result = limit_model_result(result, max_chars)
        except ModelResultTooLarge:
            model_result = ToolResult.failure('tool_result_too_large', '证据导航字段超过长度上限，请缩小读取或搜索范围', False)
            model_result.raw_result_ref = result.raw_result_ref
        return ToolExecution(execution.tool_call_id, execution.raw_result, model_result)

    async def cards(self, hits):
        from app.services.runs import atomic_thread
        rows = await atomic_thread(repository.register_sources, self.run_id, self.owner, self.kb_ids, hits)
        self.registered.update({row['handle']: row for row in rows})
        return [{**citation_card(row), 'excerpt': hit['content']} for row, hit in zip(rows, hits)]

    async def grant_input(self, messages, current, include_evidence):
        from app.services.runs import atomic_thread
        calls = {message['tool_call_id'] for message in current if message['role'] == 'tool'}
        fragments = []
        for message in messages:
            if message['role'] == 'tool' and message.get('tool_call_id') in calls:
                result = json.loads(message['content'])
                if result.get('ok'):
                    for item in (result.get('data') or {}).get('hits', (result.get('data') or {}).get('chunks', [])):
                        fragments.append((item.get('source_id'), self.chunk_id(item), item.get('content', '')))
            elif include_evidence and message in current and message['role'] == 'user':
                try:
                    evidence = json.loads(message['content']).get('evidence', [])
                except (ValueError, AttributeError):
                    continue
                fragments.extend((item.get('source_id'), item['chunk_id'], item.get('excerpt', '')) for item in evidence)
        if fragments:
            await atomic_thread(repository.grant_sources, self.run_id, self.owner, self.kb_ids, fragments)

    async def validate(self, text):
        from app.services.runs import atomic_thread
        sources = await atomic_thread(repository.current_sources, self.run_id, self.owner, self.kb_ids)
        text, citations, errors = parse_citations(text, sources)
        for error in errors:
            logger.warning('citation_validation run_id=%s handle=%s code=%s', self.run_id, error['handle'], error['code'])
        return text, citations
