import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path


def read_documents(path: Path) -> list[dict[str, str]]:
    documents = []
    with path.open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except ValueError:
                raise ValueError(f'输入第 {number} 行不是有效 JSON') from None
            fields = {'document_id', 'original_name', 'content'}
            if (not isinstance(item, dict) or set(item) != fields
                    or any(not isinstance(item[key], str) for key in fields) or not item['document_id']):
                raise ValueError(f'输入第 {number} 行必须包含 document_id/original_name/content 字符串')
            documents.append(item)
    return documents


def text_stats(content: str) -> tuple[int, Counter]:
    paragraphs = sum(bool(part.strip()) for part in re.split(r'\n[^\S\n]*\n', content.replace('\r\n', '\n')))
    keywords = Counter(word.lower() for word in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z]+', content))
    return paragraphs, keywords


def ranked(keywords: Counter) -> list[tuple[str, int]]:
    return sorted(keywords.items(), key=lambda pair: (-pair[1], pair[0]))


def markdown_cell(value: str) -> str:
    return value.replace('\\', '\\\\').replace('|', '\\|').replace('<', '&lt;').replace('>', '&gt;').replace('\r', '').replace('\n', '<br>')


def keyword_table(keywords: Counter) -> list[str]:
    return ['| 关键词 | 频次 |', '| --- | ---: |',
            *[f'| {word} | {count} |' for word, count in ranked(keywords)]]


def write_reports(documents: list[dict[str, str]], output_dir: Path) -> None:
    statistics = [text_stats(item['content']) for item in documents]
    total = Counter()
    for _, keywords in statistics:
        total.update(keywords)
    paragraphs = sum(count for count, _ in statistics)
    lines = ['# 文本统计报告', '',
        '统计规则：中文按连续汉字片段、英文按连续字母单词（忽略大小写）；按空白行分段，忽略空段。',
        '这是可解释的简化统计，不是专业 NLP；文件名和文档标识不参与统计。', '',
        f'文档数：{len(documents)}', f'总段落数：{paragraphs}', f'总词项数：{sum(total.values())}', '',
        '## 总体关键词频次', '', *keyword_table(total)]
    for number, (item, (count, keywords)) in enumerate(zip(documents, statistics), 1):
        lines.extend(['', f'## 文档 {number}', '', '| 文档标识 | 文件名 |', '| --- | --- |',
            f"| {markdown_cell(item['document_id'])} | {markdown_cell(item['original_name'])} |", '',
            f'段落数：{count}', f'词项数：{sum(keywords.values())}', '', *keyword_table(keywords)])
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with (output_dir / 'stats.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['document_id', 'original_name', 'metric', 'keyword', 'value'])
        for item, (count, keywords) in zip(documents, statistics):
            writer.writerow([item['document_id'], item['original_name'], 'paragraph_count', '', count])
            for word, frequency in ranked(keywords):
                writer.writerow([item['document_id'], item['original_name'], 'keyword_count', word, frequency])


def main() -> None:
    parser = argparse.ArgumentParser(description='统计服务提供的选定文档 JSONL 正文')
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    arguments = parser.parse_args()
    try:
        documents = read_documents(arguments.input)
        write_reports(documents, arguments.output_dir)
    except (ValueError, OSError, UnicodeError) as error:
        parser.exit(1, f'text-report 失败：{error}\n')


if __name__ == '__main__':
    main()
