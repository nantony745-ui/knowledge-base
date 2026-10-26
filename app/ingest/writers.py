import json
import os
import socket
from pathlib import Path

from app.ingest.files import document_directory


def process_start(pid: int):
    try:
        text = Path(f'/proc/{pid}/stat').read_text()
        return text[text.rfind(')')+2:].split()[19]
    except FileNotFoundError:
        return None


def register_writer(document_id, job_id):
    directory = document_directory(document_id)
    directory.mkdir(parents=True, exist_ok=True)
    record = {'pid': os.getpid(), 'start': process_start(os.getpid()), 'host': socket.gethostname(), 'job_id': job_id}
    path = directory / 'writer.json'
    temporary = directory / 'writer.tmp'
    temporary.write_text(json.dumps(record))
    temporary.replace(path)


def writer_alive(document_id):
    path = document_directory(document_id) / 'writer.json'
    if not path.exists():
        return False
    record = json.loads(path.read_text())
    if record['host'] != socket.gethostname():
        return True
    return process_start(record['pid']) == record['start'] and record['start'] is not None


def unregister_writer(document_id, job_id):
    path = document_directory(document_id) / 'writer.json'
    if path.exists() and json.loads(path.read_text())['job_id'] == job_id:
        path.unlink()
