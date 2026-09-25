from copy import deepcopy
from io import BytesIO
import json

from src.editions import enrich, read, write
from src.paper_titles import chinese_title, title_entries, title_key
from tools.build_site import paper_card
from tools.translate_paper_titles import translate


def test_title_only_translation_does_not_complete_reading_or_change_snapshot():
    row = next(row for row in title_entries().values() if row.get('doi') == '10.1016/j.ijfatigue.2026.109999')
    paper = {'id': 'b972c8c0d15c', 'title': row['title'], 'doi': row['doi'],
             'abstract': '', 'analysis_status': 'pending',
             'summary': f"本文聚焦《{row['title']}》，暂未提供可用摘要，请查看原文。"}
    payload = {'core': [], 'extended': [paper]}
    before = deepcopy(payload)
    enriched = enrich(payload, {})
    assert payload == before
    result = enriched['extended'][0]
    assert result['title_zh'] == row['title_zh']
    assert result['analysis_status'] == 'pending'
    assert enriched['analysis_status']['pending'] == 1
    assert result['id'] == paper['id']
    for root in ('./', '../'):
        html = paper_card(paper, 'extended', root=root)
        assert f'>{row["title_zh"]}<' in html
        assert f'<p class="original-title" lang="en">{paper["title"]}</p>' in html
        assert '中文精读待完成' in html
        assert '暂无可用摘要，请查看原文。' in html
        assert '本文聚焦《' not in html
        assert '<figure' not in html and 'figure-unavailable' not in html
        assert '查看示意图' not in html


def test_existing_title_wins_and_mismatched_identity_is_not_translated():
    entry = {'title': 'A test fatigue model', 'doi': '10.test/original', 'title_zh': '测试疲劳模型'}
    catalog = {title_key(entry['title']): entry}
    paper = {'title': entry['title'], 'doi': entry['doi']}
    assert chinese_title({**paper, 'title_zh': '已有中文题名'}, catalog) == '已有中文题名'
    assert chinese_title({**paper, 'title': ' A test  fatigue model '}, catalog) == entry['title_zh']
    assert chinese_title({**paper, 'title': 'A different model'}, catalog) == ''
    assert chinese_title({**paper, 'doi': '10.test/other'}, catalog) == ''
    assert chinese_title({**paper, 'doi': ''}, catalog) == entry['title_zh']
    assert chinese_title({'title': '中文论文题名'}, {}) == '中文论文题名'


def pending_paper():
    return {'id': '000000000001', 'title': 'A new physics-based fatigue model',
            'doi': '10.test/new', 'landing_url': 'https://example.org/paper',
            'abstract': '', 'analysis_status': 'pending'}


def test_automatic_title_translation_works_without_abstract_and_reuses_cache(tmp_path, monkeypatch):
    paper = pending_paper()
    write(tmp_path / 'daily.json', {'core': [], 'extended': [paper]})
    monkeypatch.setenv('LLM_API_KEY', 'test-key')
    calls = []

    def reply(request, timeout):
        calls.append(json.loads(request.data))
        text = json.dumps({'titles': [{'id': '0', 'title_zh': '一种基于物理的新疲劳模型'}]})
        return BytesIO(json.dumps({'choices': [{'message': {'content': text}}]}).encode())

    monkeypatch.setattr('tools.translate_paper_titles.urlopen', reply)
    assert translate(tmp_path) == {'missing': 1, 'translated': 1, 'status': 'ok'}
    assert read(tmp_path / 'daily.json')['extended'][0] == paper
    catalog = title_entries(tmp_path)
    assert chinese_title(paper, catalog) == '一种基于物理的新疲劳模型'
    assert 'analysis_status' not in next(iter(catalog.values()))
    assert translate(tmp_path)['status'] == 'up_to_date'
    assert len(calls) == 1


def test_unconfigured_failed_or_invalid_service_keeps_original_and_old_titles(tmp_path, monkeypatch):
    paper = pending_paper()
    write(tmp_path / 'daily.json', {'extended': [paper]})
    old = {'entries': [{'title': 'Old model', 'title_zh': '已有模型题名'}]}
    write(tmp_path / 'paper-titles.json', old)
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    assert translate(tmp_path)['status'] == 'not_configured'
    monkeypatch.setenv('LLM_API_KEY', 'test-key')

    def failed(*args, **kwargs):
        raise TimeoutError('Service unavailable')

    monkeypatch.setattr('tools.translate_paper_titles.urlopen', failed)
    assert translate(tmp_path)['status'] == 'unavailable'
    assert read(tmp_path / 'paper-titles.json') == old
    text = json.dumps({'titles': [{'id': '0', 'title_zh': 'Still English'},
                                  {'id': 'unknown', 'title_zh': '错误的模型题名'}]})
    monkeypatch.setattr('tools.translate_paper_titles.urlopen', lambda *a, **k:
        BytesIO(json.dumps({'choices': [{'message': {'content': text}}]}).encode()))
    assert translate(tmp_path)['status'] == 'partial'
    assert read(tmp_path / 'paper-titles.json') == old
    assert chinese_title(paper, title_entries(tmp_path)) == ''
