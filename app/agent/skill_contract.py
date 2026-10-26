from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.agent.tools.truncate import json_text
from app.api.common import fail, identifier
from app.db.models import Document, KnowledgeBase
from app.db.session import session
from app.db.source_repository import owned_run
from app.ingest.files import stored_file
from app.ingest.parsers import parse_text


class SkillFiles(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    run_id: str
    input: str
    report: str
    stats: str


def skill_files(run_id: str) -> SkillFiles:
    run_id = identifier(run_id)
    output = PurePosixPath('/workspace/output') / run_id
    return SkillFiles(run_id=run_id, input='/workspace/input/documents.txt',
                      report=str(output / 'report.md'), stats=str(output / 'stats.csv'))


def prepare_skill_input(owner: str, run_id: str, kb_ids: list[str], document_ids: list[str], workspace: Path) -> SkillFiles:
    files = skill_files(run_id)
    ids = list(dict.fromkeys(identifier(value) for value in document_ids))
    kb_ids = [identifier(value) for value in kb_ids]
    with session() as db:
        owned_run(db, files.run_id, owner, False)
        available_kbs = set(db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids),
            KnowledgeBase.owner_id == owner, KnowledgeBase.status == 'active')))
        if available_kbs != set(kb_ids):
            fail(404, 'kb_unavailable', '知识库不存在、不可用或无访问权限')
        rows = db.scalars(select(Document).where(Document.id.in_(ids), Document.kb_id.in_(kb_ids),
                                                 Document.parse_status == 'ready'))
        documents = {row.id: row for row in rows}
        if set(documents) != set(ids):
            fail(404, 'document_unavailable', '所选文档不存在、不可用或超出授权范围')
        content = ''.join(json_text({'document_id': value, 'original_name': documents[value].original_name,
            'content': parse_text(stored_file(documents[value].parsed_path or documents[value].original_path, value))})
            + '\n' for value in ids)
    root = workspace.resolve()
    target = (root / 'input/documents.txt').resolve()
    output = (root / 'output' / files.run_id).resolve()
    if not target.is_relative_to(root) or not output.is_relative_to(root):
        raise ValueError('技能工作区路径越界')
    target.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
    return files
