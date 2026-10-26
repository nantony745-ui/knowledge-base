from rq import Worker

from app.ingest.queue import queue, redis_connection


class IngestWorker(Worker):
    @property
    def dequeue_timeout(self):
        return min(30, super().dequeue_timeout)

    def run_maintenance_tasks(self):
        super().run_maintenance_tasks()
        from app.ingest.recovery import recover
        recover()


def main():
    from app.ingest.recovery import recover
    recover()
    from app.settings import load_settings
    IngestWorker([queue()], connection=redis_connection(), name=load_settings().INGEST_WORKER_NAME,
                 maintenance_interval=30, log_job_description=False).work()


if __name__ == '__main__':
    main()
