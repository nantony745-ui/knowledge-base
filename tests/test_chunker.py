import json
from pathlib import Path

import pytest

from app.ingest.chunker import MAX_PROTECTED_SIZE, chunk_ranges, chunk_text, overlap_start, protected_spans
from app.ingest.parsers import parse_text
from app.settings import ROOT


def test_real_corpus_coverage_lines_status_and_tables():
    manifest = json.loads((ROOT/'examples/documents/manifest.json').read_text())
    for entry in manifest:
        text = parse_text(ROOT/'examples/documents'/entry['file'])
        pieces = chunk_text(text, 512, 80)
        assert pieces
        lines = text.splitlines()
        covered = set()
        for piece in pieces:
            for first, last in piece.source_meta['line_ranges']:
                assert 1 <= first <= last <= len(lines)
                covered.update(range(first, last+1))
            assert piece.content and len(piece.content) <= 512
            if piece.source_meta['table']:
                assert '|---' in piece.content.replace(' ', '')
        assert {i+1 for i, line in enumerate(lines) if line.strip()} <= covered
        if entry['file'].startswith('archive_'):
            assert all(piece.source_meta['document_status'] == '已废止' for piece in pieces)


def test_long_paragraph_exact_character_coverage_without_boundary_overlap():
    text = '# 标题\n\n'+''.join(chr(0x4e00+i) for i in range(2000))
    pieces = chunk_text(text, 512, 80)
    assert all(character in ''.join(p.content for p in pieces) for character in text.strip() if not character.isspace())
    assert all(len(piece.content) <= 512 for piece in pieces)
    assert len(pieces) == 4


def test_heading_in_code_not_treated_as_heading():
    text = '# 真标题\n\n```python\n# 注释\n```\n\n正文'
    assert all(piece.source_meta['heading_path'] == ['真标题'] for piece in chunk_text(text, 512, 80))


def test_small_table_keeps_caption_and_answer_in_same_piece():
    text = '# SLA\n\n## 首次响应目标\n\n| 等级 | 时间 |\n|---|---|\n| P1 | 15 分钟 |\n'
    pieces = chunk_text(text, 512, 80)
    table = next(p for p in pieces if p.source_meta['table'])
    assert '首次响应目标' in table.content and '15 分钟' in table.content
    assert table.source_meta['line_start'] == 3
    assert table.source_meta['line_end'] == 7


def test_english_overlap_keeps_whole_words_and_legacy_splitter_is_unused(monkeypatch):
    def legacy_splitter(*args):
        pytest.fail('旧版切分器不应再被调用')

    monkeypatch.setattr('app.ingest.chunker.recursive_ranges', legacy_splitter)
    words = [f'word{index:04d}' for index in range(300)]
    pieces = chunk_text(' '.join(words), 512, 80)
    assert len(pieces) > 1
    assert all(word in words for piece in pieces for word in piece.content.split())
    assert set(words) == {word for piece in pieces for word in piece.content.split()}
    assert all(len(piece.content) <= 512 for piece in pieces)
    assert sum(len(piece.content.split()) for piece in pieces) == len(words)


def test_repeated_paragraphs_keep_original_line_numbers_after_heading_and_table():
    paragraph = ' '.join(f'word{index:04d}' for index in range(90))
    prefix = '# 文档\n\n## 明细\n\n| 项目 | 值 |\n|---|---|\n| A | B |\n\n'
    text = prefix + '\n\n'.join([paragraph] * 4)
    pieces = chunk_text(text, 512, 80)
    lines = text.splitlines()
    covered = set()
    for piece in pieces:
        assert piece.source_meta['heading_path'] in (['文档'], ['文档', '明细'])
        if piece.source_meta['table']:
            assert '| A | B |' in piece.content
        else:
            first, last = piece.source_meta['line_ranges'][0]
            assert piece.content in '\n'.join(lines[first-1:last])
            covered.update(range(first, last+1))
    assert {index+1 for index, line in enumerate(lines) if line == paragraph} <= covered


def test_invalid_overlap_is_rejected():
    with pytest.raises(ValueError, match='分块重叠必须小于目标长度'):
        chunk_text('正文', 512, 512)


def assert_exact_coverage(text, size, overlap):
    ranges = list(chunk_ranges(text, size, overlap))
    pieces = chunk_text(text, size, overlap)
    nonempty = [(spans, path, table) for spans, path, table in ranges
                if ''.join(text[a:b] for a, b in spans).strip()]
    assert len(pieces) == len(nonempty)
    covered = set()
    for piece, (spans, _, _) in zip(pieces, nonempty):
        assert piece.content == ''.join(text[a:b] for a, b in spans).strip()
        for a, b in spans:
            assert 0 <= a < b <= len(text)
            covered.update(range(a, b))
        assert len(piece.content) <= MAX_PROTECTED_SIZE
    assert {i for i, char in enumerate(text) if not char.isspace()} <= covered
    return pieces, nonempty


@pytest.mark.parametrize('separator', ['。', '！', '？', '. ', '! ', '? ', '；', '; ', '，', ', ', '、'])
def test_bilingual_punctuation_kept_at_end(separator):
    sentences = [chr(0x4e00+i) * 180 + separator for i in range(4)]
    text = ''.join(sentences)
    pieces, _ = assert_exact_coverage(text, 512, 0)
    assert len(pieces) == 2
    assert pieces[0].content == ''.join(sentences[:2]).strip()
    assert pieces[1].content == ''.join(sentences[2:]).strip()


def test_mixed_language_sentences_and_decimal_url_remain_intact():
    sentences = ['中文说明。', 'Version 3.14 is at https://example.com/v1.2. ',
                 '另一句话！', 'Does it work? ', 'Yes, it does! '] * 20
    text = ''.join(sentences)
    pieces, _ = assert_exact_coverage(text, 512, 80)
    assert all(piece.content.endswith(('。', '！', '？', '.', '?', '!')) for piece in pieces)
    assert sum(piece.content.count('https://example.com/v1.2.') for piece in pieces) >= 20
    assert all('3.14' in piece.content for piece in pieces if 'Version' in piece.content)


@pytest.mark.parametrize('protected', [
    '```python\n' + 'value = 123\n' * 60 + '```\n',
    '~~~text\n' + 'some code\n' * 60 + '~~~\n',
    '`' + 'value ' * 100 + '`',
    '$$\n' + 'x+y=1\n' * 100 + '$$',
    '[说明](https://example.com/' + 'a' * 600 + ')',
    '![说明](https://example.com/' + 'b' * 600 + ')',
])
def test_oversize_protected_content_is_atomic(protected):
    text = '前文。' * 100 + '\n\n' + protected + '\n\n' + '后文。' * 100
    pieces, _ = assert_exact_coverage(text, 512, 80)
    assert any(piece.content == protected.strip() for piece in pieces)
    assert all(len(piece.content) <= 512 or piece.content == protected.strip() for piece in pieces)


@pytest.mark.parametrize('protected', [
    '```text\n' + '代码\n' * 3000 + '```\n',
    '$$' + 'x' * 16000 + '$$',
    '[link](https://example.com/' + 'a ' * 8000 + ')',
])
def test_protected_content_over_hard_limit_is_split_without_loss(protected):
    pieces, ranges = assert_exact_coverage(protected, 512, 80)
    assert len(pieces) > 1
    assert all(len(piece.content) <= 7500 for piece in pieces)
    assert all(a[0][0][1] == b[0][0][0] for a, b in zip(ranges, ranges[1:]))


def test_forced_protected_remainder_stays_independent_of_following_prose():
    protected = '$$' + 'x' * 7500 + '$$'
    text = protected + '正常正文。'
    pieces, _ = assert_exact_coverage(text, 512, 80)
    assert [piece.content for piece in pieces] == [protected[:7500], protected[7500:], '正常正文。']


@pytest.mark.parametrize('length', [512, 513, 7500, 7501])
def test_protected_size_thresholds(length):
    protected = '$$' + 'x' * (length-4) + '$$'
    pieces, _ = assert_exact_coverage(protected, 512, 80)
    assert [piece.content for piece in pieces] == [protected[i:i+7500] for i in range(0, length, 7500)]


def test_code_and_formula_fake_structure_is_ignored():
    text = '# 真标题\n\n```markdown\n## 假标题\n\n| A | B |\n|---|---|\n| C | D |\n```\n\n'
    text += '$$\n## 公式内部\n| A | B |\n|---|---|\n| C | D |\n$$\n\n真实正文。'
    pieces, _ = assert_exact_coverage(text, 512, 80)
    assert all(piece.source_meta['heading_path'] == ['真标题'] for piece in pieces)
    assert not any(piece.source_meta['table'] for piece in pieces)


@pytest.mark.parametrize('formula', [
    '$$\n| 公式行 | 值 |\n$$',
    '$$表达式\n| 公式行 | 值 |\n$$',
])
def test_table_candidate_cannot_cut_multiline_protection(formula):
    text = '| 项目 | 值 |\n|---|---|\n| ' + formula + ' | 结果 |\n' + '后文。' * 200
    pieces, ranges = assert_exact_coverage(text, 512, 80)
    assert any(formula in piece.content for piece in pieces)
    assert not any(piece.source_meta['table'] for piece in pieces)
    assert not any(a < boundary < b for a, b in protected_spans(text)
                   for spans, _, _ in ranges for span in spans for boundary in span)


def test_real_heading_and_table_can_contain_inline_code_and_links():
    header = '| 项目 | 值 |\n|---|---|\n'
    row = '| `code` | [link](https://example.com) |\n'
    text = '# `真实标题`\n\n' + header + row * 30
    pieces, _ = assert_exact_coverage(text, 512, 80)
    tables = [piece for piece in pieces if piece.source_meta['table']]
    assert len(tables) >= 3
    assert all(piece.source_meta['heading_path'] == ['`真实标题`'] for piece in pieces)
    assert all(piece.content.startswith(header.strip()) for piece in tables)


def test_nested_link_and_inline_code_with_multiple_backticks_are_protected():
    text = '``code ` nested`` and [link](https://example.com/a_(b))'
    assert [text[a:b] for a, b in protected_spans(text)] == [
        '``code ` nested``', '[link](https://example.com/a_(b))']


def test_tail_window_finds_boundary_inside_large_paragraph():
    first = '甲' * 270 + '。' + '乙' * 29 + '！'
    second = '丙' * 249 + '。'
    text = first + '\n' + second
    pieces, ranges = assert_exact_coverage(text, 512, 80)
    assert len(pieces) == 2
    assert ranges[0][0] == [(0, len(first) + 1)]
    assert ranges[1][0][0][0] == 271
    assert pieces[1].content.startswith('乙' * 29 + '！')


@pytest.mark.parametrize('text,budget,expected', [
    ('开始。甲甲\n\n尾部。更多', 80, '尾部。更多'),
    ('开始。甲甲\n尾部。更多', 80, '尾部。更多'),
    ('开始。中间！末尾', 80, '中间！末尾'),
    ('First. Middle? Last!', 80, 'Middle? Last!'),
    ('aaa\r\n\r\nbbbb', 4, 'bbbb'),
    ('aaa\n\nbbbb', 4, 'bbbb'),
    ('没有任何标点', 80, ''),
    ('结束。   ', 80, ''),
    ('开始。尾部', 0, ''),
])
def test_overlap_boundary_priority_and_lookbehind(text, budget, expected):
    start = overlap_start(text, 0, len(text), budget, protected_spans(text))
    assert text[start:] == expected
    assert len(text[start:]) <= budget


def test_overlap_does_not_use_protected_boundaries_or_split_atomic_content():
    text = '句子。`内部。内容`'
    start = overlap_start(text, 0, len(text), 80, protected_spans(text))
    assert text[start:] == '`内部。内容`'
    assert overlap_start(text, 0, len(text), 5, protected_spans(text)) == len(text)


def test_overlap_shrinks_to_next_unit_budget():
    text = '甲' * 250 + '。' + '乙' * 49 + '！' + '丙' * 499 + '。'
    pieces, ranges = assert_exact_coverage(text, 512, 80)
    assert len(pieces) == 2
    assert ranges[1][0][0][0] == ranges[0][0][0][1]


def test_oversize_table_rows_repeat_header_within_hard_limit():
    header = '| 项目 | 值 |\n|---|---|\n'
    text = '# 表格\n\n' + header + '| ' + '甲' * 16000 + ' | 值 |\n'
    pieces, _ = assert_exact_coverage(text, 512, 80)
    tables = [piece for piece in pieces if piece.source_meta['table']]
    assert len(tables) >= 3
    assert all(piece.content.startswith(header.strip()) for piece in tables)
    assert all(len(piece.content) <= 7500 for piece in tables)


def test_table_header_at_hard_limit_falls_back_without_repetition():
    header = '| ' + '甲' * 7488 + ' |\n|---|---|\n'
    assert len(header) >= 7500
    text = header + '| A | B |\n' * 3
    pieces, ranges = assert_exact_coverage(text, 512, 80)
    assert all(piece.source_meta['table'] for piece in pieces)
    assert all(a[0][-1][1] == b[0][0][0] for a, b in zip(ranges, ranges[1:]))


def test_heading_and_table_boundaries_have_no_overlap():
    text = '# 一\n\n第一段。\n\n## 二\n\n第二段。\n\n| A | B |\n|---|---|\n| C | D |\n\n' + '正文。' * 200
    _, ranges = assert_exact_coverage(text, 512, 80)
    for (left, lpath, ltable), (right, rpath, rtable) in zip(ranges, ranges[1:]):
        if lpath != rpath or ltable or rtable:
            assert left[-1][1] <= right[-1][0]
