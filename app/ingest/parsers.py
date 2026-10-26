import re
from pathlib import Path

from app.adapters.common import AdapterError


def parse_text(path: Path) -> str:
    try:
        text = path.read_text(encoding='utf-8-sig')
    except UnicodeDecodeError:
        raise AdapterError('invalid_encoding', False, '文本不是有效UTF-8编码', None) from None
    if not text.strip() or '\x00' in text:
        raise AdapterError('invalid_text', False, '正文为空或包含二进制内容', None)
    return text


def metadata(text: str) -> dict:
    fields = {}
    title = None
    for line in text.splitlines():
        if line.startswith('## '):
            break
        if line.startswith('# ') and title is None:
            title = line[2:].strip()
        match = re.match(r'^- ([^：]+)：(.+)$', line)
        if match:
            fields[match[1]] = match[2].strip().replace('**', '')
    return {'document_title': title, 'document_metadata': fields, 'document_status': fields.get('状态'), 'parser': 'text-v1'}
