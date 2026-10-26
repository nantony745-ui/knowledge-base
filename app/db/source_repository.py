from sqlalchemy import select
from fastapi import HTTPException

from app.api.common import fail, identifier
from app.db.models import Chunk, ConversationSession, Document, RunSource, StoredRun
from app.db.session import session
from app.rag.retrieval import ready_statement


def owned_run(db, run_id, owner, lock):
    statement = select(StoredRun).join(ConversationSession).where(
        StoredRun.id == identifier(run_id), ConversationSession.owner_id == owner)
    row = db.scalar(statement.with_for_update() if lock else statement)
    if row is None:
        fail(404, 'run_not_found', '运行不存在或无访问权限')
    return row


def source_dict(row):
    from app.agent.sources import SourceReference
    return SourceReference.model_validate({'run_id': row.run_id, 'handle': row.handle, 'chunk_id': row.chunk_id,
            'document_id': row.document_id, 'source_snapshot': row.source_snapshot,
            'evidence_granted': row.evidence_granted}).model_dump(mode='json')


def register_sources(run_id, owner, kb_ids, items):
    from app.agent.sources import SourceSnapshot
    with session() as db:
        owned_run(db, run_id, owner, True)
        rows = list(db.scalars(select(RunSource).where(RunSource.run_id == run_id)))
        found = {row.chunk_id: row for row in rows if row.chunk_id}
        number = max((int(row.handle[1:]) for row in rows), default=0)
        chunk_ids, invalid = [], None
        for item in items:
            try:
                chunk_ids.append(identifier(item['chunk_id']))
            except (HTTPException, KeyError, TypeError, AttributeError) as exc:
                invalid = exc
                break
        current = {row.chunk_id: row for row in db.execute(
            ready_statement(owner, kb_ids).with_only_columns(
                Chunk.id.label('chunk_id'), Document.id.label('document_id'), Document.original_name,
                Chunk.page_start, Chunk.page_end, Document.kb_id).where(Chunk.id.in_(set(chunk_ids))))} if chunk_ids else {}
        output = []
        for chunk_id in chunk_ids:
            source = current.get(chunk_id)
            if source is None:
                fail(404, 'chunk_unavailable', '片段不存在、不可用或无访问权限')
            row = found.get(chunk_id)
            if row is None:
                number += 1
                snapshot = SourceSnapshot(original_name=source.original_name, page_start=source.page_start,
                    page_end=source.page_end, excerpt='', kb_id=source.kb_id)
                row = RunSource(run_id=run_id, handle=f's{number}', chunk_id=chunk_id,
                    document_id=source.document_id, source_snapshot=snapshot.model_dump(mode='json'), evidence_granted=False)
                db.add(row)
                found[chunk_id] = row
            output.append(source_dict(row))
        # 先处理此前的有效 ID，保持“前项不存在”优先于“后项格式错误”。
        if invalid is not None:
            raise invalid
        db.commit()
        return output


def grant_sources(run_id, owner, kb_ids, fragments):
    with session() as db:
        owned_run(db, run_id, owner, True)
        rows = list(db.scalars(select(RunSource).where(RunSource.run_id == run_id)))
        found = {row.handle: row for row in rows}
        fragments = list(fragments)
        chunk_ids = {chunk_id for handle, chunk_id, excerpt in fragments
                     if handle in found and found[handle].chunk_id == chunk_id and excerpt.strip()}
        current = dict(db.execute(ready_statement(owner, kb_ids).with_only_columns(
            Chunk.id, Document.id).where(Chunk.id.in_(chunk_ids))).all()) if chunk_ids else {}
        for handle, chunk_id, excerpt in fragments:
            row = found.get(handle)
            if row is None or row.chunk_id != chunk_id or not excerpt.strip():
                continue
            if chunk_id not in current or current[chunk_id] != row.document_id:
                continue
            row.evidence_granted = True
            row.source_snapshot = {**row.source_snapshot, 'excerpt': excerpt}
        db.commit()


def current_sources(run_id, owner, kb_ids):
    from app.db.models import Chunk
    with session() as db:
        owned_run(db, run_id, owner, False)
        rows = list(db.scalars(select(RunSource).where(RunSource.run_id == run_id)))
        chunks = list(db.execute(ready_statement(owner, kb_ids).where(
            Chunk.id.in_([row.chunk_id for row in rows if row.chunk_id]))))
        readable = {chunk.id: document.id for chunk, document in chunks}
        return [{**source_dict(row), 'readable': row.chunk_id in readable and
                 readable[row.chunk_id] == row.document_id} for row in rows]


def get_source(run_id, owner, handle):
    from app.db.models import Chunk
    with session() as db:
        run_id = identifier(run_id)
        owned_run(db, run_id, owner, False)
        row = db.get(RunSource, (run_id, handle))
        if row is None:
            fail(404, 'source_not_found', '来源不存在')
        deleted = row.chunk_id is None or row.document_id is None
        current = None if deleted else db.execute(ready_statement(owner, [row.source_snapshot['kb_id']])
            .where(Chunk.id == row.chunk_id)).first()
        return {**source_dict(row), 'original_deleted': deleted,
                'readable': current is not None and current[1].id == row.document_id}
