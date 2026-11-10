from pathlib import Path
import codecs

import pytest
from fastapi.testclient import TestClient

from yuecang.api import create_app
from yuecang.application.service import Workbench
from yuecang.core.encoding import decode, preview, repair_text
from yuecang.jobs import JobQueue
from yuecang.storage.project import DomainError
from yuecang.storage.maintenance import verify_project


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'gb18030', 'big5', 'utf-16', 'utf-16-le', 'utf-16-be', 'utf-32', 'utf-32-le', 'utf-32-be'])
def test_explicit_decoding_is_exact(encoding):
    text = '繁體中文\r\nEnglish\n'
    raw = text.encode(encoding)
    assert decode(raw, encoding)[0] == text
    assert preview(raw, encoding)['preview'] == text


@pytest.mark.parametrize('encoding', ['utf-16-le', 'utf-16-be', 'utf-32-le', 'utf-32-be'])
def test_bomless_unicode_detects_byte_order_with_ascii_evidence(encoding):
    text = '第一章\nChapter 1\n中文內容。\n'
    assert decode(text.encode(encoding)) == (text, encoding, True)


def test_bomless_utf16_prefers_the_stronger_byte_order_evidence():
    text = '一A\n一B\n'
    assert decode(text.encode('utf-16-be')) == (text, 'utf-16-be', True)


def test_big5_ambiguity_is_visible_and_explicit_choice_corrects_it():
    raw = '繁體中文'.encode('big5')
    report = preview(raw)
    assert any(i['code'] == 'encoding_ambiguous' for i in report['issues'])
    assert next(x for x in report['encoding_candidates'] if x['encoding'] == 'big5')['preview'] == '繁體中文'
    assert preview(raw, 'big5')['preview'] == '繁體中文'


@pytest.mark.parametrize('wrong_codec,repair', [('latin-1', 'utf8-as-latin1'), ('cp1252', 'utf8-as-cp1252'), ('gb18030', 'utf8-as-gb18030')])
def test_reversible_mojibake_repairs_without_replacing_original(wrong_codec, repair):
    good = '中文'
    wrong = good.encode('utf-8').decode(wrong_codec)
    assert repair_text(wrong, repair) == (good, 1)
    text = f'正常正文 ABC {wrong} ABC 正常正文'
    assert repair_text(text, repair)[0] == '正常正文 ABC 中文 ABC 正常正文'
    assert repair_text('正常正文 ABC', repair)[0] == '正常正文 ABC'


@pytest.mark.parametrize('text,repair,expected', [
    ('€Ã©', 'utf8-as-latin1', '€é'),
    ('―Ã©', 'utf8-as-cp1252', '―é'),
])
def test_repair_preserves_adjacent_characters_outside_the_selected_codec(text, repair, expected):
    assert repair_text(text, repair) == (expected, 1)


def test_irrecoverable_and_normalization_are_explicit():
    report = preview('缺失�文字\u200b'.encode())
    assert report['preview'] == '缺失�文字\u200b'
    assert {i['code'] for i in report['issues']} >= {'lost_encoding_characters', 'invisible_characters'}
    assert repair_text('e\u0301', 'nfc')[0] == 'é'
    assert repair_text('a\u200bb\ufeffc\u200d', 'remove-invisible')[0] == 'abc\u200d'
    with pytest.raises(ValueError):
        preview(b'abc', 'unicode_escape')
    with pytest.raises(UnicodeError):
        preview(b'\xff', 'utf-8')


def test_redecode_creates_new_source_preserves_assignments_and_draft(tmp_path):
    wb = Workbench(tmp_path / 'library')
    path = tmp_path / 'book.txt'
    raw = '繁體中文'.encode('big5')
    path.write_bytes(raw)
    old = wb.source(wb.import_file(path)['source_id'])
    book = wb.create_book('合成编码测试')
    spans = [{'source_id': old['id'], 'block_id': b['id']} for b in old['blocks']]
    wb.assign(book['book']['id'], spans=spans, edition_id=book['edition']['id'])
    draft = wb.assemble(book['book']['id'], spans, edition_id=book['edition']['id'])
    report = wb.text_preview(old['id'], encoding='big5')
    assert report['preview'] == '繁體中文'
    result = wb.import_file(wb.project.object_path(old['asset_sha']), name=old['name'], encoding='big5',
                            derived_from=old['id'], expected_blocks_sha=report['expected_blocks_sha'])
    new = wb.source(result['source_id'])
    assert new['id'] != old['id'] and new['asset_sha'] == old['asset_sha']
    assert ''.join(b['text'] for b in new['blocks']) == '繁體中文'
    assert wb.source(old['id'])['blocks_sha'] == old['blocks_sha']
    assert wb.revision(draft['revision_id'])['blocks'] == draft['revision']['blocks']
    assert wb.project.object_path(new['asset_sha']).read_bytes() == raw
    assert len(wb.state()['assignments']) == len(spans)
    assert verify_project(wb.project)['ok']
    with pytest.raises(DomainError, match='预览'):
        wb.import_file(path, encoding='big5', derived_from=old['id'], expected_blocks_sha='stale')


def test_api_recovery_requires_preview_hash_and_job_is_idempotent(tmp_path):
    app = create_app(tmp_path / 'library', start_worker=False)
    wb = app.state.workbench
    path = tmp_path / 'broken.txt'
    path.write_text('cafÃ©', encoding='utf-8')
    source = wb.import_file(path)['source_id']
    client = TestClient(app)
    headers = {'X-Yuecang-CSRF': client.get('/api/session').json()['csrf_token']}
    base = f'/api/sources/{source}'
    opts = {'encoding': 'utf-8', 'repair': 'utf8-as-latin1'}
    result = client.post(base + '/text-preview', json=opts, headers=headers)
    assert result.status_code == 200 and result.json()['preview'] == 'café'
    payload = {**opts, 'expected_blocks_sha': result.json()['expected_blocks_sha']}
    assert client.post(base + '/text-apply', json={**payload, 'expected_blocks_sha': 'stale'}, headers=headers).status_code == 409
    first = client.post(base + '/text-apply', json=payload, headers=headers).json()['job']
    again = client.post(base + '/text-apply', json=payload, headers=headers).json()['job']
    assert first['id'] == again['id']
    queue = JobQueue(wb.project)
    queue.run_one()
    job = queue.get(first['id'])
    assert job['state'] == 'succeeded', job
    repaired = wb.source(job['result']['source_id'])
    assert repaired['metadata']['derived_from'] == source
    assert ''.join(b['text'] for b in repaired['blocks']) == 'café'


def test_txt_headings_and_bounded_blocks_preserve_every_codepoint(tmp_path):
    from yuecang.formats import extract
    text = '《合成教材》\r\n第一章 植物\r\n' + '中文😀正文。' * 3000 + '\r\n第五章 观察\r\n内容\r\n第一章 附录\r\n结束'
    path = tmp_path / 'long.txt'
    path.write_bytes(text.encode())
    result = extract(path, resource_dir=tmp_path / 'resources')
    assert ''.join(b['text'] for b in result.blocks) == text
    assert max(len(b['text']) for b in result.blocks) <= 4096
    assert len([b for b in result.blocks if b['type'] == 'heading']) == 4
    for block in result.blocks:
        assert text[block['locator']['start']:block['locator']['end']] == block['text']
