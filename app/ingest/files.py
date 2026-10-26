from pathlib import Path
from uuid import UUID

from app.api.common import fail
from app.settings import load_settings


def document_directory(document_id: str) -> Path:
    normalized = str(UUID(document_id))
    root = load_settings().DATA_DIR.resolve()
    directory = root / 'documents' / normalized
    if not directory.resolve().is_relative_to(root / 'documents') or directory.is_symlink():
        fail(409, 'unsafe_path', '文档存储路径不安全')
    return directory


def stored_file(relative: str, document_id: str) -> Path:
    path = (load_settings().DATA_DIR / relative).resolve()
    if not path.is_relative_to(document_directory(document_id).resolve()):
        fail(409, 'unsafe_path', '文件不属于此文档目录')
    return path
