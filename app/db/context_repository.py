from uuid import uuid4

from sqlalchemy import select

from app.adapters.common import AdapterError
from app.db.models import ContextCheckpoint, Message, StoredRun
from app.db.session import session
from app.services.conversations import message_dict, owned_session


def latest_checkpoint(db, session_id):
    return db.scalar(select(ContextCheckpoint).where(ContextCheckpoint.session_id == session_id)
        .order_by(ContextCheckpoint.covered_message_seq.desc()).limit(1))


class ContextHistory(list):
    def __init__(self, messages, checkpoint, sources=()):
        super().__init__(messages)
        self.checkpoint = checkpoint
        self.sources = list(sources)


def load_context(session_id, owner):
    from app.agent.context import ContextSummary
    with session() as db:
        owned_session(db, session_id, owner, True)
        checkpoint = latest_checkpoint(db, session_id)
        summary = ContextSummary.model_validate_json(checkpoint.summary) if checkpoint else None
        covered = checkpoint.covered_message_seq if checkpoint else 0
        if summary is not None and summary.covered_message_seq != covered:
            raise AdapterError('invalid_checkpoint', False, '摘要覆盖边界不一致', None)
        rows = db.scalars(select(Message).where(Message.session_id == session_id, Message.seq > covered)
                          .order_by(Message.seq))
        return ContextHistory([message_dict(row) for row in rows], summary, checkpoint.sources if checkpoint else [])


def save_checkpoint(session_id, owner, previous_seq, groups, summary, max_chars):
    from app.agent.context import SummarySource, validate_summary
    from app.agent.history import group_history, navigation_sources
    with session() as db:
        owned_session(db, session_id, owner, True)
        boundary = summary.covered_message_seq
        existing = db.scalar(select(ContextCheckpoint).where(ContextCheckpoint.session_id == session_id,
            ContextCheckpoint.covered_message_seq == boundary))
        if existing:
            from app.agent.context import ContextSummary
            return ContextSummary.model_validate_json(existing.summary)
        previous = latest_checkpoint(db, session_id)
        if (previous.covered_message_seq if previous else 0) != previous_seq:
            raise AdapterError('checkpoint_changed', True, '摘要检查点已变化', None)
        rows = [message_dict(row) for row in db.scalars(select(Message).where(
            Message.session_id == session_id, Message.seq > previous_seq).order_by(Message.seq))]
        covered = [row for row in rows if row['seq'] <= boundary]
        actual = group_history(covered, max_chars)
        if not actual or actual[-1].end_seq != boundary or [(g.turn_id, g.start_seq, g.end_seq) for g in actual] != [
                (g.turn_id, g.start_seq, g.end_seq) for g in groups]:
            raise AdapterError('invalid_checkpoint_boundary', False, '摘要覆盖范围不是连续完整历史', None)
        if any(g.messages[-1]['role'] != 'assistant' or g.messages[-1].get('tool_calls') for g in actual):
            raise AdapterError('invalid_checkpoint_boundary', False, '摘要包含尚未结束的turn', None)
        if any(row['turn_id'] == actual[-1].turn_id and row['seq'] > boundary for row in rows):
            raise AdapterError('invalid_checkpoint_boundary', False, '摘要边界截断了turn', None)
        prior = None
        if previous:
            from app.agent.context import ContextSummary
            prior = ContextSummary.model_validate_json(previous.summary)
        validated = validate_summary(summary.model_dump_json(), actual, prior)
        sources = {s.get('chunk_id') or s['document_id']: SummarySource.model_validate({k: s[k] for k in ('chunk_id', 'document_id') if k in s}).model_dump(mode='json')
                   for s in (previous.sources if previous else []) + navigation_sources(covered)}
        db.add(ContextCheckpoint(id=str(uuid4()), session_id=session_id, summary=validated.model_dump_json(),
            covered_message_seq=boundary, sources=list(sources.values())))
        db.commit()
        return validated


def register_run(session_id, owner, run_id):
    with session() as db:
        owned_session(db, session_id, owner, True)
        db.add(StoredRun(id=run_id, session_id=session_id))
        db.commit()
