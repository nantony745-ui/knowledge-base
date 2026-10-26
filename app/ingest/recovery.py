from datetime import datetime, timezone

from rq import Worker
from rq.exceptions import NoSuchJobError
from rq.job import Job
from sqlalchemy import select

from app.db.models import Document, KnowledgeBase
from app.db.session import session
from app.ingest.jobs import mark_failed
from app.ingest.queue import redis_connection
from app.ingest.writers import writer_alive
from app.settings import load_settings


def job_for(job_id):
    if not job_id:
        return None
    try:
        job = Job.fetch(job_id, connection=redis_connection())
    except NoSuchJobError:
        return None
    return job if job.origin == load_settings().INGEST_QUEUE else None


def live_execution(job):
    if job is None:
        return False
    now = datetime.now(timezone.utc)
    executions = job.get_executions()
    if any((now-e.last_heartbeat).total_seconds() < 90 for e in executions):
        return True
    if job.worker_name:
        worker = Worker.find_by_key('rq:worker:'+job.worker_name, connection=redis_connection())
        if worker is not None and worker.last_heartbeat and worker.get_current_job_id() == job.id:
            heartbeat = worker.last_heartbeat
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            return (now-heartbeat).total_seconds() < 90
    return False


def recover():
    s = load_settings()
    now = datetime.now(timezone.utc)
    with session() as db:
        docs = list(db.scalars(select(Document).join(KnowledgeBase, Document.kb_id == KnowledgeBase.id)
                               .where(KnowledgeBase.status == 'active', Document.parse_status.in_(['queued', 'parsing', 'indexing']))))
    recovered = []
    for doc in docs:
        if doc.stage == 'waiting_indexer':
            continue
        age = (now-doc.updated_at.replace(tzinfo=timezone.utc)).total_seconds()
        job = job_for(doc.job_id)
        status = job.get_status(refresh=True).value if job else None
        if writer_alive(doc.id) or live_execution(job) or redis_connection().exists('personal_knora:document:'+doc.id):
            continue
        if status in {'queued', 'deferred', 'scheduled'}:
            continue
        if status is None and age <= s.ENQUEUE_GRACE_SECONDS:
            continue
        if status == 'started' and age < 90:
            continue
        if status == 'started':
            job.cancel()
        code = 'enqueue_interrupted' if status is None and doc.parse_status == 'queued' else 'interrupted'
        mark_failed(doc.id, doc.job_id, code, '任务入队中断' if code == 'enqueue_interrupted' else '任务已终止或失联，可重试')
        recovered.append(doc.id)
    return recovered
