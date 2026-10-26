import re
from bisect import bisect_right
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.ingest.parsers import metadata


MAX_PROTECTED_SIZE = 7500
SEPARATORS = ['\n\n', '\n', '。', '！', '？', '. ', '! ', '? ', '；', '; ', '，', ', ', '、', ' ', '']


class DeferredMergeSplitter(RecursiveCharacterTextSplitter):
    """保留 LangChain 递归单元，由项目统一合并正文与保护片段。"""

    def _merge_splits(self, splits, separator):
        return list(splits)


@dataclass
class Piece:
    content: str
    title: str | None
    source_meta: dict


def recursive_ranges(text: str, start: int, end: int, size: int, overlap: int):
    """旧版自定义切分实现，已停用；保留用于对照，当前使用 LangChain。"""
    cursor = start
    while cursor < end:
        stop = min(cursor + size, end)
        if stop < end:
            for separator in ('\n\n', '\n', '。', '；', ' ', ''):
                boundary = text.rfind(separator, cursor + size // 2, stop) if separator else -1
                if boundary >= 0:
                    stop = boundary + len(separator)
                    break
        if text[cursor:stop].strip():
            yield cursor, stop
        if stop == end:
            break
        cursor = max(cursor + 1, stop - overlap)


def langchain_ranges(text: str, start: int, end: int, splitter: RecursiveCharacterTextSplitter):
    for part in splitter.create_documents([text[start:end]]):
        offset = start + part.metadata['start_index']
        yield offset, offset + len(part.page_content)


def protected_spans(text: str) -> list[tuple[int, int]]:
    spans, cursor = [], 0
    opening = re.compile(r'^ {0,3}(`{3,}|~{3,})[^\n]*\n', re.M)
    while match := opening.search(text, cursor):
        fence = match[1]
        closing = re.compile(r'^ {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*(?:\n|$)', re.M)
        end = closing.search(text, match.end())
        cursor = end.end() if end else match.end()
        if end:
            spans.append((match.start(), end.end()))
    patterns = (
        r'\$\$[\s\S]*?\$\$',
        r'(?<!`)(`+)(?!`)[^\n]*?(?<!`)\1(?!`)',
        r'!?\[[^\]\n]*\]\((?:[^()\n]|\([^()\n]*\))*\)',
    )
    for pattern in patterns:
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text))
    merged = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def intersects(start: int, end: int, protected: list[tuple[int, int]]) -> bool:
    return any(start < b and a < end for a, b in protected)


def sections(text: str, protected: list[tuple[int, int]]):
    headings, stack, offset = [], [], 0
    for line in text.splitlines(keepends=True):
        heading = None if any(a <= offset < b for a, b in protected) else re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
        if heading:
            level = len(heading[1])
            stack = [entry for entry in stack if entry[0] < level] + [(level, heading[2])]
            headings.append((offset, [entry[1] for entry in stack]))
        offset += len(line)
    if not headings or headings[0][0] != 0:
        headings.insert(0, (0, []))
    return [(start, headings[index+1][0] if index+1 < len(headings) else len(text), path)
            for index, (start, path) in enumerate(headings)]


def forced_ranges(text: str, start: int, end: int, limit: int):
    while end - start > limit:
        stop = start + limit
        boundary = text.rfind('\n', start, stop)
        if boundary < start:
            boundary = text.rfind(' ', start, stop)
        stop = boundary + 1 if boundary >= start else stop
        yield start, stop
        start = stop
    if start < end:
        yield start, end


def table_groups(text: str, start: int, end: int, size: int, protected: list[tuple[int, int]]):
    pattern = re.compile(r'^\|[^\n]+\|\s*\n\|[\s:|\-]+\|[^\n]*\n(?:\|[^\n]+\|[^\n]*(?:\n|$))+', re.M)
    cursor = start
    for table in pattern.finditer(text, start, end):
        if any(a <= table.start() < b for a, b in protected):
            continue
        boundaries = [table.start() + match.end() for match in re.finditer('\n', table.group())] + [table.end()]
        if any(a < boundary < b for a, b in protected for boundary in boundaries):
            continue
        if cursor < table.start():
            yield [(cursor, table.start())], False
        lines = table.group().splitlines(keepends=True)
        header_end = table.start() + len(lines[0]) + len(lines[1])
        header = (table.start(), header_end)
        header_length = header_end - table.start()
        if header_length >= MAX_PROTECTED_SIZE:
            for span in forced_ranges(text, table.start(), table.end(), MAX_PROTECTED_SIZE):
                yield [span], True
            cursor = table.end()
            continue
        rows, length, row_start = [], header_end-table.start(), header_end
        for line in lines[2:]:
            if rows and length + len(line) > size:
                yield [header, *rows], True
                rows, length = [], header_end-table.start()
            if header_length + len(line) > MAX_PROTECTED_SIZE:
                for span in forced_ranges(text, row_start, row_start + len(line), MAX_PROTECTED_SIZE - header_length):
                    yield [header, span], True
                row_start += len(line)
                continue
            rows.append((row_start, row_start+len(line)))
            length += len(line)
            row_start += len(line)
        if rows:
            yield [header, *rows], True
        cursor = table.end()
    if cursor < end:
        yield [(cursor, end)], False


def plain_units(text: str, start: int, end: int, protected: list[tuple[int, int]], splitter: DeferredMergeSplitter):
    cursor = start
    for a, b in protected:
        if b <= start or a >= end:
            continue
        if cursor < a:
            yield from langchain_ranges(text, cursor, a, splitter)
        yield from forced_ranges(text, a, b, MAX_PROTECTED_SIZE)
        cursor = b
    if cursor < end:
        yield from langchain_ranges(text, cursor, end, splitter)


def overlap_start(text: str, start: int, end: int, budget: int, protected: list[tuple[int, int]]) -> int:
    if budget <= 0:
        return end
    window_start = max(start, end - budget)
    candidates = []
    for match in re.finditer(r'\r\n\r\n|\n\n|\r\n|\n|[。？！]|[.?!] ', text[max(start, window_start - 4):end]):
        a = max(start, window_start - 4) + match.start()
        b = max(start, window_start - 4) + match.end()
        if b < window_start or not text[b:end].strip() or intersects(a, b, protected):
            continue
        separator = match.group()
        priority = 0 if separator in ('\n\n', '\r\n\r\n') else 1 if separator in ('\n', '\r\n') else 2
        candidates.append((priority, a, b))
    return min(candidates)[2] if candidates else end


def merge_units(text: str, units, size: int, overlap: int, protected: list[tuple[int, int]]):
    current = None
    for start, end in units:
        independent = any(b-a > size and a <= start and end <= b for a, b in protected)
        if end - start > size or independent:
            if current:
                yield current
            yield start, end
            current = None
        elif current is None:
            current = (start, end)
        elif end - current[0] <= size:
            current = (current[0], end)
        else:
            yield current
            budget = min(overlap, size - (end - start))
            tail = overlap_start(text, *current, budget, protected)
            current = (tail, end)
    if current:
        yield current


def chunk_ranges(text: str, size: int, overlap: int):
    if overlap >= size:
        raise ValueError('分块重叠必须小于目标长度')
    if overlap < 0:
        raise ValueError('分块重叠不能小于0')
    splitter = DeferredMergeSplitter(chunk_size=size, chunk_overlap=0, separators=SEPARATORS,
                                    keep_separator='end', add_start_index=True, strip_whitespace=False)
    protected = protected_spans(text)
    for start, end, path in sections(text, protected):
        blocks = list(table_groups(text, start, end, size, protected))
        if end-start <= size:
            blocks = [([(start, end)], any(table for _, table in blocks))]
        for spans, table in blocks:
            if table:
                yield spans, path, True
            else:
                units = plain_units(text, spans[0][0], spans[0][1], protected, splitter)
                for span in merge_units(text, units, size, overlap, protected):
                    yield [span], path, False


def chunk_text(text: str, size: int, overlap: int) -> list[Piece]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer('\n', text))
    document_meta = metadata(text)
    result = []
    for group, path, table in chunk_ranges(text, size, overlap):
        content = ''.join(text[a:b] for a, b in group).strip()
        if not content:
            continue
        ranges = [[bisect_right(starts, a), bisect_right(starts, max(a, b-1))] for a, b in group]
        meta = {**document_meta, 'heading_path': path, 'line_ranges': ranges,
                'line_start': min(a for a, b in ranges), 'line_end': max(b for a, b in ranges), 'table': table}
        result.append(Piece(content, (path[-1] if path else document_meta['document_title']), meta))
    return result
