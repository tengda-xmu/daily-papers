"""Link recovery must lead to body verification, never just a nicer label."""
from datetime import timedelta
import json

from src import coverage_links, paper_coverage as coverage
from src.public_sources import read
from src.wechat_metadata import public_index_url
from tests.test_paper_coverage import NOW, PAPER, URL, page, setup


def test_private_redirect_recovers_into_verified_article_after_service_backoff(tmp_path, monkeypatch):
    setup(tmp_path)
    calls, resolved = [], []
    index_url = public_index_url('Research account', PAPER['title'])
    def index(query, _):
        calls.append(query)
        return {'organic_results': [{'link': index_url, 'title': PAPER['title'],
            'account': 'Research account', 'reason': 'index_only',
            '_redirect_url': 'https://weixin.sogou.com/link?url=PRIVATE_TOKEN'}],
            'link_resolution': {'state': 'blocked', 'retry_after': 60}}
    monkeypatch.setattr(coverage, 'model_summary', lambda *_: None)
    monkeypatch.setattr(coverage_links, '_resolve', lambda url: resolved.append(url) or URL)
    first = coverage.collect(tmp_path, NOW, key='', index_searcher=index)
    assert not first['papers'][PAPER['id']]['articles'] and not resolved
    tasks = list((tmp_path / coverage_links.PRIVATE / PAPER['id']).glob('*.json'))
    assert len(tasks) == 1 and 'PRIVATE_TOKEN' in read(tasks[0])['redirect_url']
    before = len(calls)
    second = coverage.collect(tmp_path, NOW + timedelta(seconds=61), key='', index_searcher=index,
                              fetcher=lambda *_: page())
    row = second['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['articles'][0]['url'] == URL
    assert row['articles'][0]['match_basis'] == 'doi' and not row['candidates']
    assert len(resolved) == 1 and len(calls) == before
    assert 'PRIVATE_TOKEN' not in json.dumps(second) and 'redirect_url' not in json.dumps(second)


def test_expired_redirect_refreshes_by_article_title_then_verifies_body(tmp_path, monkeypatch):
    setup(tmp_path)
    coverage_links.remember(tmp_path, PAPER, {'title': PAPER['title'], 'account': 'Research account',
        '_redirect_url': 'https://weixin.sogou.com/link?url=expired'}, NOW)
    monkeypatch.setattr(coverage_links, '_resolve', lambda *_: '')
    monkeypatch.setattr(coverage, 'model_summary', lambda *_: None)
    calls = []
    def index(query, _):
        calls.append(query)
        return {'organic_results': [{'link': URL, 'title': PAPER['title']}]}
    row = coverage.collect(tmp_path, NOW, key='', index_searcher=index,
                           fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['articles'][0]['url'] == URL
    assert len(calls) == 1 and 'Research account' in calls[0] and PAPER['title'] in calls[0]


def test_resolved_link_without_readable_body_never_becomes_verified(tmp_path, monkeypatch):
    setup(tmp_path)
    coverage_links.remember(tmp_path, PAPER, {'title': PAPER['title'],
        '_redirect_url': 'https://weixin.sogou.com/link?url=unverified'}, NOW)
    monkeypatch.setattr(coverage_links, '_resolve', lambda *_: URL)
    row = coverage.collect(tmp_path, NOW, key='', index_searcher=lambda *_: {'organic_results': []},
        fetcher=lambda *_: b'<html>wappoc_appmsgcaptcha</html>')['papers'][PAPER['id']]
    assert not row['articles']
    assert row['candidates'][0]['url'] == URL
    assert row['candidates'][0]['body_status'] == 'challenge'
