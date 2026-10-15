from copy import deepcopy
from datetime import timedelta
import json

import pytest

from src import paper_coverage as coverage
from src.coverage_search_ledger import PRIVATE, import_results, load_queries
from src.public_sources import read, write
from tests.test_paper_coverage import NOW, PAPER, URL, page, setup


def package(papers=None, state='ok', results=None):
    return {'version': 1, 'provider': 'codex_session', 'search_strategy_version': coverage.SEARCH_VERSION,
            'queries': [{'paper_id': paper['id'], 'query': '"' + paper['doi'] + '"',
                         'checked_at': NOW.isoformat(), 'state': state,
                         'results': results if results is not None else [{'url': URL, 'title': 'Research article'}]}
                        for paper in papers or [PAPER]]}


def test_import_is_private_idempotent_and_retains_actual_attempts(tmp_path):
    payload = package()
    assert len(import_results(tmp_path, payload, [PAPER['id']], coverage.SEARCH_VERSION, NOW)) == 1
    path = next((tmp_path / PRIVATE).glob('*.json'))
    modified = path.stat().st_mtime_ns
    assert import_results(tmp_path, payload, [PAPER['id']], coverage.SEARCH_VERSION, NOW) == []
    assert path.stat().st_mtime_ns == modified
    later = deepcopy(payload)
    later['queries'][0].update(checked_at=(NOW + timedelta(days=1)).isoformat(), state='blocked', results=[])
    import_results(tmp_path, later, [PAPER['id']], coverage.SEARCH_VERSION, NOW + timedelta(days=1))
    assert len(read(path)['attempts']) == 2
    assert load_queries(tmp_path)[PAPER['id']][0]['state'] == 'blocked'
    assert load_queries(tmp_path)[PAPER['id']][0]['discovered_results'][0]['url'] == URL
    assert not (tmp_path / coverage.DATA).exists()


@pytest.mark.parametrize('change', [
    {'paper_id': 'unknown'}, {'query': ''}, {'checked_at': '2026-10-13'},
    {'checked_at': (NOW + timedelta(days=1)).isoformat()}, {'state': 'pending'},
    {'results': [{'url': 'http://user:password@example.com'}]}, {'results': None},
])
def test_invalid_batch_is_rejected_before_any_write(tmp_path, change):
    payload = package()
    payload['queries'].append({**payload['queries'][0], **change})
    with pytest.raises(ValueError):
        import_results(tmp_path, payload, [PAPER['id']], coverage.SEARCH_VERSION, NOW)
    assert not (tmp_path / PRIVATE).exists()


def test_multiple_papers_process_imports_despite_provider_and_negative_caches(tmp_path):
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other'}
    setup(tmp_path, [PAPER, other])
    write(tmp_path / coverage.DATA, {'papers': {p['id']: {'state': 'no_match', 'search_version': coverage.SEARCH_VERSION,
        'last_search_at': NOW.isoformat(), 'articles': [], 'candidates': []} for p in (PAPER, other)}})
    coverage._cached_article(tmp_path, URL, NOW, lambda *_: b'<html>captcha</html>')
    write(tmp_path / coverage.CACHE / 'providers/serpapi.json', {'state': 'blocked',
        'retry_at': (NOW + timedelta(days=1)).isoformat()})
    payload = package([PAPER, other])
    def forbid(*_):
        pytest.fail('Imported sources must not require a new search')
    result = coverage.collect(tmp_path, NOW, searcher=forbid, index_searcher=forbid, perform_search=False,
        imported_search_results=payload, fetcher=lambda *_: page('介绍正文。' * 30 + ' DOI: 10.1234/fixture DOI: 10.1234/other'),
        summarizer=lambda *_: None)
    for row in result['papers'].values():
        assert row['state'] == 'matched'
        assert row['search_providers']['codex_session']['queries_completed'] == 1
        assert row['last_search_at'] == NOW.isoformat()
    assert not list((tmp_path / coverage.CACHE / 'searches').glob('*.json'))


def test_no_search_reads_existing_sources_and_never_upgrades_unsearched_version(tmp_path):
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'no_match', 'search_version': 6,
        'articles': [], 'candidates': [{'url': URL, 'reason': 'identity_unconfirmed'}]}}})
    result = coverage.collect(tmp_path, NOW, perform_search=False, fetcher=lambda *_: page(), summarizer=lambda *_: None)
    row = result['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['search_version'] == 6
    assert not row.get('last_search_at')


def test_provider_cooldown_is_not_a_completed_strategy_upgrade(tmp_path):
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'no_match', 'search_version': 6,
        'articles': [], 'candidates': []}}})
    write(tmp_path / coverage.CACHE / 'providers/serpapi.json', {'state': 'blocked',
        'retry_at': (NOW + timedelta(days=1)).isoformat()})
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: pytest.fail('Backoff'))['papers'][PAPER['id']]
    assert row['search_version'] == 6 and not row.get('last_search_at')
    assert not list((tmp_path / PRIVATE).glob('*.json'))


def test_snippets_do_not_confirm_and_reimport_does_not_repeat_negative_fetch(tmp_path):
    setup(tmp_path)
    payload = package(results=[{'url': URL, 'title': PAPER['title'], 'snippet': PAPER['doi']}])
    calls = []
    def fetch(*_):
        calls.append(1)
        return page('别的论文内容。' * 30)
    for _ in range(2):
        row = coverage.collect(tmp_path, NOW, perform_search=False, imported_search_results=payload,
            fetcher=fetch, summarizer=lambda *_: None)['papers'][PAPER['id']]
        assert not row['articles']
        assert 'snippet' not in json.dumps(row)
    assert calls == [1]


def test_actual_search_records_query_in_ledger(tmp_path):
    setup(tmp_path)
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': [{'link': URL}]},
        fetcher=lambda *_: page(), summarizer=lambda *_: None)['papers'][PAPER['id']]
    assert row['state'] == 'matched'
    records = load_queries(tmp_path, provider='serpapi', strategy=coverage.SEARCH_VERSION)
    assert records[PAPER['id']][0]['query'] == coverage._queries(PAPER)[0]
    assert records[PAPER['id']][0]['results'][0]['url'] == URL


@pytest.mark.parametrize('outcome,expected', [('blocked', 'unavailable'), ('error', 'error'), ('ok', 'no_match')])
def test_imported_failure_is_not_an_empty_success(tmp_path, outcome, expected):
    setup(tmp_path)
    row = coverage.collect(tmp_path, NOW, perform_search=False,
        imported_search_results=package(state=outcome, results=[]))['papers'][PAPER['id']]
    assert row['state'] == expected
    assert row['search_providers']['codex_session']['queries_completed'] == int(outcome == 'ok')


def test_source_only_without_queries_cannot_claim_no_match(tmp_path):
    setup(tmp_path)
    row = coverage.collect(tmp_path, NOW, perform_search=False)['papers'][PAPER['id']]
    assert row['state'] == 'deferred' and row['search_status'] == 'not_searched'
    assert not row.get('last_search_at')


@pytest.mark.parametrize('url', [
    'https://doi.org/10.1038/s41467-026-78118-3', 'https://arxiv.org/abs/2609.12345',
    'https://arxiv.org/pdf/2609.12345', 'https://www.nature.com/articles/s41467-026-78118-3',
    'https://www.sciencedirect.com/science/article/pii/S0142112326000459',
    'https://pubmed.ncbi.nlm.nih.gov/12345678/', 'https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/',
    'https://www.researchgate.net/publication/12345_Paper', 'https://patents.google.com/patent/CN12345A/en',
    'https://example.com/paper.PDF',
])
def test_primary_resources_stay_in_ledger_without_source_fetch(tmp_path, url):
    from src.coverage_search_ledger import DECISIONS
    setup(tmp_path)
    row = coverage.collect(tmp_path, NOW, perform_search=False,
        imported_search_results=package(results=[{'url': url, 'title': 'Primary paper'}]),
        fetcher=lambda *_: pytest.fail('Must not fetch a primary paper as WeChat coverage'))['papers'][PAPER['id']]
    assert not row['candidates'] and not row['articles']
    assert row['search_providers']['codex_session']['queries_completed'] == 1
    assert load_queries(tmp_path)[PAPER['id']][0]['results'][0]['url'] == url
    decision = read(next((tmp_path / DECISIONS).glob('*.json')))
    assert decision['decision'] == 'skip_source_read' and decision['reason']


@pytest.mark.parametrize('url', [
    'https://www.nature.com/articles/d41586-026-12345-1', 'https://www.nature.com/news/research-feature',
    'https://cloud.tencent.com/developer/article/2656311', 'https://hub.baai.ac.cn/view/53949',
    'https://kaiwu.qboson.com/forum.php?mod=viewthread&tid=3533', 'https://example.com/blog/paper-review',
    'https://mp.weixin.qq.com/s/article',
])
def test_primary_filter_keeps_news_blogs_and_synchronized_reading(url):
    from src.coverage_search_ledger import academic_source_reason
    assert not academic_source_reason(url)
