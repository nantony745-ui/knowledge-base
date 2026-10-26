from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from app.adapters.common import AdapterError
from app.agent.messages import history_window, interrupted_envelope, tool_summary
from app.api.common import fail, identifier
from app.db.models import ConversationSession, Message
from app.db.session import session


def owned_session(db, session_id: str, owner: str, lock: bool = False):
    statement = select(ConversationSession).where(ConversationSession.id == identifier(session_id),
                                                 ConversationSession.owner_id == owner)
    row = db.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        fail(404, 'session_not_found', '会话不存在')
    return row


def check_session(session_id, owner):
    with session() as db:
        owned_session(db, session_id, owner)


def message_dict(message):
    return {'id':message.id, 'session_id':message.session_id, 'seq':message.seq,
            'turn_id':message.turn_id, 'role':message.role, 'content':message.content,
            'tool_calls':message.tool_calls, 'tool_call_id':message.tool_call_id,
            'run_id':message.run_id, 'status':message.status, 'citations':message.citations or [],
            'created_at':message.created_at.replace(tzinfo=timezone.utc).isoformat()}


def _insert_message(db, conversation, role, content, turn_id, status, citations,
                    run_id, tool_calls, tool_call_id):
    message_id = str(uuid4())
    message = Message(id=message_id, session_id=conversation.id, seq=conversation.next_message_seq,
        turn_id=turn_id or message_id, role=role, content=content, status=status,
        citations=citations, run_id=run_id, tool_calls=tool_calls, tool_call_id=tool_call_id)
    conversation.next_message_seq += 1
    conversation.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(message)
    return message


def _turn_rows(db, session_id, turn_id):
    statement = select(Message).where(Message.session_id == session_id).order_by(Message.seq)
    if turn_id is not None:
        statement = statement.where(Message.turn_id == turn_id)
    return list(db.scalars(statement))


def _pending_calls(rows):
    calls, results = {}, {}
    for row in rows:
        if row.role == 'assistant' and row.status == 'complete':
            for call in row.tool_calls or []:
                key = (row.turn_id, call['id'])
                if key in calls:
                    raise AdapterError('invalid_message_history', False, '历史工具调用ID重复', None)
                calls[key] = row
        if row.role == 'tool':
            key = (row.turn_id, row.tool_call_id)
            if key in results:
                raise AdapterError('invalid_message_history', False, '历史工具返回重复', None)
            results[key] = row
    return [(key, row) for key, row in calls.items() if key not in results]


def append_message(session_id, owner, role, content, turn_id, status, citations,
                   *, run_id=None, tool_calls=None, tool_call_id=None):
    with session() as db:
        conversation = owned_session(db, session_id, owner, True)
        if role in ('assistant', 'tool'):
            rows = _turn_rows(db, conversation.id, turn_id)
            pending = _pending_calls(rows)
            if role == 'tool':
                existing = next((row for row in rows if row.role == 'tool'
                                 and row.tool_call_id == tool_call_id), None)
                if existing is not None:
                    return message_dict(existing)
                if not pending or pending[0][0][1] != tool_call_id:
                    raise AdapterError('invalid_tool_result', False, '工具返回没有按顺序匹配调用', None)
            elif pending:
                raise AdapterError('unpaired_tool_calls', False, '上一批工具调用尚未全部返回', None)
            if tool_calls:
                ids = [call['id'] for call in tool_calls]
                used = {call['id'] for row in rows for call in row.tool_calls or []}
                if len(set(ids)) != len(ids) or used.intersection(ids):
                    raise AdapterError('invalid_tool_calls', False, '工具调用ID重复', None)
        message = _insert_message(db, conversation, role, content, turn_id, status, citations,
                                  run_id, tool_calls, tool_call_id)
        db.commit(); db.refresh(message)
        return message_dict(message)


def append_run_message(session_id, owner, role, content, turn_id, status, citations,
                       run_id, tool_calls, tool_call_id):
    return append_message(session_id, owner, role, content, turn_id, status, citations,
                          run_id=run_id, tool_calls=tool_calls, tool_call_id=tool_call_id)


def repair_tool_results(session_id, owner, turn_id, code):
    with session() as db:
        conversation = owned_session(db, session_id, owner, True)
        pending = _pending_calls(_turn_rows(db, conversation.id, turn_id))
        saved = []
        for (call_turn, call_id), assistant in pending:
            saved.append(_insert_message(db, conversation, 'tool', interrupted_envelope(code),
                call_turn, 'complete', None, assistant.run_id, None, call_id))
        db.commit()
        return [message_dict(row) for row in saved]


def list_messages(session_id, owner):
    with session() as db:
        owned_session(db, session_id, owner)
        rows = [message_dict(m) for m in _turn_rows(db, session_id, None)]
    names = {}
    for row in rows:
        for call in row['tool_calls'] or []:
            names[(row['turn_id'], call['id'])] = call['function']['name']
        if row['role'] == 'tool':
            row['tool_name'] = names.get((row['turn_id'], row['tool_call_id']), '')
            row['tool_summary'] = tool_summary(row['content'])
    return rows


def recent_history(session_id, owner, turns):
    repair_tool_results(session_id, owner, None, 'interrupted')
    return history_window(list_messages(session_id, owner), turns)


def delete_history(session_id, owner):
    with session() as db:
        row = owned_session(db, session_id, owner, True)
        db.delete(row); db.commit()
