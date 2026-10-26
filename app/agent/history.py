import copy
import hashlib
import json
import re
from dataclasses import dataclass

from app.adapters.common import AdapterError
from app.agent.refs import RunRefs
from app.agent.messages import model_message, protocol_complete
from app.agent.tools.truncate import json_text, truncate_result


@dataclass(frozen=True)
class TurnGroup:
    turn_id: str
    start_seq: int
    end_seq: int
    messages: list[dict]
    chunk_ids: frozenset[str]
    artifacts: frozenset[tuple[str, str]]


def load_history(session_id: str, owner: str) -> list[dict]:
    from app.db.context_repository import load_context
    return load_context(session_id, owner)


def navigation_sources(rows: list[dict]) -> list[dict]:
    found = {}
    for row in rows:
        sources = (row.get('citations') or []) if row['role'] == 'assistant' and row['status'] == 'complete' else []
        if row['role'] == 'tool' and row['status'] == 'complete':
            sources = sources + (json.loads(row['content'])['raw_result'].get('sources') or [])
        for source in sources:
            identifier = source.get('chunk_id') or source.get('document_id')
            if identifier:
                found[identifier] = {key: value for key, value in source.items() if key in {
                    'chunk_id', 'document_id', 'kb_id', 'original_name', 'page_start', 'page_end', 'title'}}
    return list(found.values())


def artifact_references(value) -> set[tuple[str, str]]:
    references = set()
    if isinstance(value, dict):
        if isinstance(value.get('artifact_id'), str) and isinstance(value.get('path'), str):
            references.add((value['artifact_id'], value['path']))
        value = list(value.values())
    if isinstance(value, list):
        for item in value:
            references.update(artifact_references(item))
    return references


def without_citation_ids(value):
    if isinstance(value, list):
        return [without_citation_ids(item) for item in value]
    if isinstance(value, dict):
        return {key: without_citation_ids(item) for key, item in value.items() if key != 'source_id'}
    return value


def history_ref_maps(rows: list[dict]) -> dict:
    aliases = {}
    for row in rows:
        scope = row.get('run_id') or row['turn_id']
        mapping = aliases.setdefault(scope, {})
        if row['role'] != 'tool' or row['status'] != 'complete':
            continue
        envelope = json.loads(row['content'])
        raw, model = envelope['raw_result'].get('data') or {}, envelope['model_result'].get('data') or {}
        pairs = [(raw, model)]
        for raw_key, model_key in (('documents', 'documents'), ('reranked', 'hits'), ('hits', 'hits'), ('chunks', 'chunks')):
            pairs.extend(zip(raw.get(raw_key, []), model.get(model_key, [])))
        for internal, exposed in pairs:
            source = {**internal.get('source', {}), **internal}
            for key, kind in (('document_id', 'd'), ('chunk_id', 'c'), ('target_chunk_id', 'c')):
                identifier = source.get(key)
                if identifier:
                    for handle in (exposed.get(key.replace('_id', '_ref')), exposed.get('source_id')):
                        if handle:
                            mapping[(kind, handle)] = identifier
    return aliases


def reproject_history(value, refs: RunRefs, aliases: dict):
    if isinstance(value, list):
        return [reproject_history(item, refs, aliases) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == 'source_id':
            continue
        kind = {'document_id': 'd', 'document_ref': 'd', 'chunk_id': 'c', 'chunk_ref': 'c',
                'target_chunk_id': 'c', 'target_chunk_ref': 'c'}.get(key)
        if kind:
            identifier = aliases.get((kind, item)) if key.endswith('_ref') or re.fullmatch(r's\d+', item or '') else item
            result[key.replace('_id', '_ref')] = refs.register(kind, identifier) if identifier else None
        else:
            result[key] = reproject_history(item, refs, aliases)
    return result


def project_turn(rows: list[dict], max_chars: int, refs=None, aliases=None) -> TurnGroup:
    complete = [model_message(row) for row in rows if row['status'] == 'complete' and row['role'] != 'system']
    if complete and (complete[0]['role'] != 'user' or sum(message['role'] == 'user' for message in complete) != 1
                     or not protocol_complete(complete)):
        raise AdapterError('invalid_message_history', False, '历史turn或工具调用配对无效', None)
    messages, mapping, tool_names = [], {}, {}
    prefix = 'history_' + hashlib.sha256(rows[0]['turn_id'].encode()).hexdigest()[:16]
    for row in rows:
        if row['status'] != 'complete':
            messages.append({'role': 'assistant', 'content': json_text({
                'public_history_marker': row['status'],
                'text': re.sub(r'\[s\d+\]', '[历史来源：需重新读取]', row['content'] or ''), 'executable': False})})
            continue
        if row['role'] == 'system':
            messages.append({'role': 'user', 'content': json_text({'historical_system_data': row['content']})})
            continue
        message = copy.deepcopy(model_message(row))
        if message['role'] == 'assistant' and message.get('content'):
            message['content'] = re.sub(r'\[s\d+\]', '[历史来源：需重新读取]', message['content'])
        for call in message.get('tool_calls') or []:
            tool_names[call['id']] = call['function']['name']
            if refs is not None and call['function']['name'] in {'read_document', 'read_chunk'}:
                try:
                    arguments = json.loads(call['function']['arguments'])
                except ValueError:
                    arguments = None
                if isinstance(arguments, dict):
                    call['function']['arguments'] = json_text(reproject_history(
                        arguments, refs, aliases.get(row.get('run_id') or row['turn_id'], {})))
            mapping[call['id']] = f'{prefix}_{len(mapping)}'
            call['id'] = mapping[call['id']]
        if message['role'] == 'tool':
            message['tool_call_id'] = mapping[message['tool_call_id']]
            value = without_citation_ids(json.loads(message['content']))
            if refs is not None and tool_names[row['tool_call_id']] in {
                    'list_documents', 'search_knowledge', 'read_document', 'read_chunk'}:
                value = reproject_history(value, refs, aliases.get(row.get('run_id') or row['turn_id'], {}))
            message['content'] = json_text(truncate_result(value, max_chars))
        messages.append(message)
    sources = navigation_sources(rows)
    artifacts = set()
    for row in rows:
        if row['role'] == 'tool' and row['status'] == 'complete':
            artifacts.update(artifact_references(json.loads(row['content'])['raw_result'].get('data')))
    if sources:
        navigation = refs.project(sources) if refs is not None else sources
        messages[0]['content'] = (messages[0].get('content') or '') + '\n' + json_text({'history_sources': navigation})
    return TurnGroup(rows[0]['turn_id'], rows[0]['seq'], rows[-1]['seq'], messages,
                     frozenset(source['chunk_id'] for source in sources if source.get('chunk_id')), frozenset(artifacts))


def group_history(rows: list[dict], max_chars: int, refs=None) -> list[TurnGroup]:
    ordered = sorted(rows, key=lambda row: row['seq'])
    if len({row['seq'] for row in ordered}) != len(ordered):
        raise AdapterError('invalid_message_history', False, '历史消息seq重复', None)
    groups, seen = [], set()
    for row in ordered:
        if not groups or groups[-1][0]['turn_id'] != row['turn_id']:
            if row['turn_id'] in seen:
                raise AdapterError('invalid_message_history', False, '历史turn消息不连续', None)
            seen.add(row['turn_id'])
            groups.append([])
        groups[-1].append(row)
    try:
        aliases = history_ref_maps(ordered) if refs is not None else None
        return [project_turn(group, max_chars, refs, aliases) for group in groups]
    except (KeyError, TypeError, ValueError):
        raise AdapterError('invalid_message_history', False, '历史消息结构无效', None) from None
