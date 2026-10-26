from functools import lru_cache

from redis import Redis
from rq import Queue

from app.settings import load_settings


@lru_cache
def redis_connection():
    s = load_settings()
    s.require('REDIS_PASSWORD')
    return Redis(host=s.REDIS_HOST, port=s.REDIS_PORT, db=s.REDIS_DB,
                 password=s.REDIS_PASSWORD.get_secret_value(), socket_connect_timeout=5, socket_timeout=5)


def queue():
    return Queue(load_settings().INGEST_QUEUE, connection=redis_connection())


def enqueue(document_id: str, job_id: str):
    queue().enqueue_call(func='app.ingest.jobs.ingest_document', args=(document_id, job_id),
                         job_id=job_id, timeout=load_settings().JOB_TIMEOUT, result_ttl=86400, failure_ttl=604800)
