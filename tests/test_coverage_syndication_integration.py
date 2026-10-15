"""Public copies resolve real account sources without trusting search snippets."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json

import pytest

from src import paper_coverage as coverage
from src.public_sources import read, write
from tests.test_paper_coverage import NOW, PAPER, URL, setup, page

MIRROR = 'https://hub.baai.ac.cn/view/53949'


@pytest.fixture
def records(monkeypatch):
    rows = [{'url': URL, 'title': '论文方法介绍', 'account': '科研公众号', 'author': '研究团队',
             'published_at': '2026-04-14', 'body_status': 'readable',
             'source_version': hashlib.sha256(b'private body').hexdigest(),
             'mirror_url': MIRROR, 'mirror_kind': 'article', 'mirror_body_status': 'readable',
             'evidence_source': 'syndicated_article',
             'match_text': PAPER['title'] + ' Trishitha Tirupathi', 'blocks': [], 'image_count': 0}]
    monkeypatch.setattr('src.coverage_syndication.parse_syndication', lambda *_: deepcopy(rows))
    monkeypatch.setattr(coverage, 'model_summary', lambda *_: None)
    return rows


def fetcher(url, _):
    return b'<html>captcha</html>' if 'mp.weixin.qq.com' in url else b'private body'


def test_unrestricted_search_finds_copy_and_captcha_cannot_remove_association(tmp_path, records):
    setup(tmp_path)
    queries = []
    def search(query, _):
        queries.append(query)
        return {'organic_results': [] if query.startswith('site:') else [{'link': MIRROR, 'title': '研究介绍'}]}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=fetcher)
    article = result['papers'][PAPER['id']]['articles'][0]
    assert article['url'] == URL and article['verification_method'] == 'syndication'
    assert article['match_basis'] == 'title_author' and article['body_status'] == 'challenge'
    assert article['mirrors'][0]['url'] == MIRROR and article['mirrors'][0]['body_status'] == 'readable'
    assert queries[0].startswith('site:') and not queries[-1].startswith('site:')
    assert 'private body' not in json.dumps(result) and 'blocks' not in article


def test_new_copy_hint_bypasses_negative_cache_and_both_provider_backoffs(tmp_path, records):
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'no_match',
        'search_version': coverage.SEARCH_VERSION, 'last_search_at': NOW.isoformat()}}})
    for provider in ('serpapi', 'sogou'):
        write(tmp_path / coverage.CACHE / 'providers' / (provider + '.json'), {
            'scope': 'index' if provider == 'sogou' else 'search', 'state': 'blocked',
            'retry_at': (NOW + timedelta(days=1)).isoformat()})
    calls = []
    row = coverage.collect(tmp_path, NOW, key='key', paper_ids=[PAPER['id']], source_urls=[MIRROR],
        searcher=lambda *_: pytest.fail('Known copy must not wait for a provider'),
        fetcher=lambda u, s: calls.append(u) or fetcher(u, s))['papers'][PAPER['id']]
    assert row['state'] == 'matched' and MIRROR in calls and URL in calls
    assert row['articles'][0]['body_status'] == 'challenge'
    later = coverage.collect(tmp_path, NOW + timedelta(days=8), key='',
        fetcher=lambda *_: (_ for _ in ()).throw(OSError('offline')))['papers'][PAPER['id']]
    assert later['state'] == 'matched' and later['articles'][0]['url'] == URL


def test_copy_without_body_identity_cannot_be_confirmed_by_search_snippet(tmp_path, records):
    setup(tmp_path)
    records[0]['match_text'] = 'Unrelated topic ' * 20
    records[0]['title'] = '其他研究'
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': [
        {'link': MIRROR, 'snippet': PAPER['title'] + ' Trishitha Tirupathi'}]}, fetcher=fetcher)['papers'][PAPER['id']]
    assert not row['articles']


def test_two_reports_from_one_feed_are_separately_confirmed(tmp_path, records):
    setup(tmp_path)
    records.append({**records[0], 'url': URL + '-second', 'title': '第二篇介绍', 'account': '另一公众号'})
    result = coverage.collect(tmp_path, NOW, key='', paper_ids=[PAPER['id']], source_urls=[MIRROR], fetcher=fetcher)
    assert {a['url'] for a in result['papers'][PAPER['id']]['articles']} == {URL, URL + '-second'}


def test_image_copy_enters_private_visual_queue_without_auto_confirmation(tmp_path, records):
    setup(tmp_path)
    records[0].update(title=PAPER['title'], match_text='研究方法说明。', image_count=1,
        blocks=[{'type': 'image', 'remote_url': 'https://simg.baai.ac.cn/paper.webp',
                 'position': 0, 'reference_section': False, 'context_before': '本研究'}])
    result = coverage.collect(tmp_path, NOW, key='', paper_ids=[PAPER['id']], source_urls=[MIRROR], fetcher=fetcher)
    row = result['papers'][PAPER['id']]
    assert not row['articles'] and row['candidates']
    assert row['candidates'][0]['reason'] == 'visual_pending'
    assert row['candidates'][0]['body_status'] == 'challenge'
    tasks = list((tmp_path / coverage.CACHE / 'visual/tasks').glob('*.json'))
    assert len(tasks) == 1 and read(tasks[0])['article']['blocks'][0]['context_before'] == '本研究'
    assert read(tasks[0])['article']['body_status'] != 'readable'
    assert 'remote_url' not in json.dumps(result)


def test_broad_search_retains_other_public_sites_as_unverified_leads(tmp_path, records):
    rows, state = coverage._search(tmp_path, 'drug optimization', NOW, 'key',
        lambda *_: {'organic_results': [{'link': 'https://example.org/paper'}]})
    assert len(rows) == 1 and state == 'ok'
    assert rows[0]['url'] == 'https://example.org/paper'
    assert rows[0].get('match_status') != 'verified'
    assert read(tmp_path / coverage.CACHE / 'providers/serpapi.json')['state'] == 'ok'


def test_image_copy_mirror_survives_later_confirmation_from_original_body(tmp_path, records):
    setup(tmp_path)
    records[0].update(title=PAPER['title'], match_text='研究方法说明。', image_count=1,
        blocks=[{'type': 'image', 'remote_url': 'https://simg.baai.ac.cn/paper.webp',
                 'position': 0, 'reference_section': False}])
    result = coverage.collect(tmp_path, NOW, key='', paper_ids=[PAPER['id']], source_urls=[MIRROR],
        fetcher=lambda url, _: page() if 'mp.weixin.qq.com' in url else b'private body')
    article = result['papers'][PAPER['id']]['articles'][0]
    assert article['verification_method'] == 'body' and article['body_status'] == 'readable'
    assert article['mirrors'][0]['url'] == MIRROR


def test_new_strategy_rechecks_previous_strategy_empty_cache(tmp_path, records):
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'version': 2, 'papers': {PAPER['id']: {
        'state': 'no_match', 'search_version': 5, 'last_search_at': NOW.isoformat()}}})
    calls = []
    coverage.collect(tmp_path, NOW, key='key', searcher=lambda q, _: calls.append(q) or {'organic_results': []})
    assert any(not q.startswith('site:') for q in calls)
    assert len(calls) > 2
