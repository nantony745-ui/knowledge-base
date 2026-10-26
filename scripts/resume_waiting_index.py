from uuid import uuid4
from sqlalchemy import select

from app.db.models import Document, KnowledgeBase
from app.db.session import session
from app.ingest.files import stored_file
from app.ingest.locks import document_lock
from app.ingest.recovery import job_for, live_execution


def main():
    with session() as db:
        ids = list(db.scalars(select(Document.id).join(KnowledgeBase, Document.kb_id == KnowledgeBase.id).where(
            KnowledgeBase.status == 'active', Document.parse_status == 'indexing', Document.stage == 'waiting_indexer')))
    for document_id in ids:
        with document_lock(document_id):
            with session() as db:
                doc = db.get(Document, document_id)
                kb = db.get(KnowledgeBase, doc.kb_id, with_for_update=True)
                db.refresh(doc, with_for_update=True)
                job = job_for(doc.job_id)
                if kb.status != 'active' or doc.stage != 'waiting_indexer' or live_execution(job):
                    continue
                if job and job.get_status().value in {'queued', 'started', 'deferred', 'scheduled'}:
                    continue
                if not doc.parsed_path or not stored_file(doc.parsed_path, doc.id).is_file():
                    continue
                doc.job_id, doc.parse_status, doc.stage = str(uuid4()), 'queued', 'enqueue'
                db.commit()
                job_id = doc.job_id
            from app.api.documents import enqueue_saved
            enqueue_saved(document_id, job_id)
            print(document_id, job_id)


if __name__ == '__main__':
    main()
