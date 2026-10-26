from contextlib import contextmanager
from threading import Event, Thread

from redis.exceptions import LockError, RedisError

from app.api.common import fail
from app.ingest.queue import redis_connection
from app.settings import load_settings


class LeaseLost(RuntimeError):
    pass


class Lease:
    def __init__(self, document_id: str):
        self.seconds = load_settings().LOCK_LEASE_SECONDS
        self.lock = redis_connection().lock('personal_knora:document:'+document_id,
                                            timeout=self.seconds, blocking_timeout=1, thread_local=False)
        self.stop = Event()
        self.lost = Event()

    def renew(self):
        while not self.stop.wait(self.seconds / 3):
            try:
                self.lock.extend(self.seconds, replace_ttl=True)
            except (RedisError, LockError):
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set() or not self.lock.owned():
            raise LeaseLost('文档执行锁已失效')


@contextmanager
def document_lock(document_id: str):
    lease = Lease(document_id)
    if not lease.lock.acquire():
        fail(409, 'document_busy', '文档任务尚在运行，请稍后重试', True)
    thread = Thread(target=lease.renew, daemon=True)
    thread.start()
    try:
        yield lease
    finally:
        lease.stop.set()
        thread.join(timeout=6)
        try:
            lease.lock.release()
        except (RedisError, LockError):
            lease.lost.set()
