import io
import json
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from src.sources import elsevier
from src.sources.elsevier import ElsevierAdapter

WINDOW = (datetime(2021, 9, 24, tzinfo=timezone.utc), datetime(2026, 9, 24, 23, 59, 59, tzinfo=timezone.utc))


def entry(n, date='2026-09-24'):
    return {'dc:title': f'Kriging study {n}', 'prism:coverDate': date,
            'prism:doi': f'10.1234/{n}', 'dc:identifier': f'SCOPUS_ID:{n}'}


def response(entries, total):
    return io.BytesIO(json.dumps({'search-results': {
        'opensearch:totalResults': str(total), 'entry': entries}}).encode())


def adapter(limit=10):
    return ElsevierAdapter(api_key='secret-fixture', queries=['TITLE-ABS-KEY("Kriging")'], manual=True, limit=limit)


def test_manual_scopus_uses_relevance_and_pages_after_future_issue_dates(monkeypatch):
    calls = []
    def fetch(request, timeout):
        params = parse_qs(urlsplit(request.full_url).query)
        calls.append(params)
        assert 'apiKey' not in params
        if params['start'] == ['0']:
            return response([entry(i, '2026-12-01') for i in range(25)], 12229)
        return response([entry(i + 25) for i in range(25)], 12229)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    rows = source.fetch(*WINDOW)
    assert len(rows) == 10 and all(r.published_at == '2026-09-24' for r in rows)
    assert [p['start'] for p in calls] == [['0'], ['25']]
    assert all(p['sort'] == ['relevancy'] and p['view'] == ['STANDARD'] and p['date'] == ['2021-2026'] for p in calls)
    assert all(p['query'] == ['TITLE-ABS-KEY("Kriging")'] for p in calls)
    assert source.status.status == 'ok' and '2 页、50 条' in source.status.message
    assert '12229' in source.status.message


def test_bounded_scan_is_partial_instead_of_false_no_data(monkeypatch):
    calls = []
    def fetch(request, timeout):
        start = int(parse_qs(urlsplit(request.full_url).query)['start'][0])
        calls.append(start)
        return response([entry(i + start, '2026-12-01') for i in range(25)], 2000)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    assert source.fetch(*WINDOW) == []
    assert calls == [0, 25, 50]
    assert source.status.status == 'partial' and '3 页检索上限' in source.status.message


def test_real_empty_search_and_narrow_date_range(monkeypatch):
    monkeypatch.setenv('CNS_LOOKBACK_DAYS', '180')
    def fetch(request, timeout):
        assert parse_qs(urlsplit(request.full_url).query)['date'] == ['2026']
        return response([entry(1, '2026-08-01') | {'prism:publicationName': 'Nature'}, entry(2, '2026-09-25')], 2)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    assert source.fetch(datetime(2026,9,24,tzinfo=timezone.utc), WINDOW[1]) == []
    assert source.status.status == 'no_data'  # All candidates scanned; no CNS lookback exception.
    monkeypatch.setattr(elsevier, 'urlopen', lambda *a, **kw: response([{'error':'RESULT_NOT_FOUND'}], 0))
    assert source.fetch(*WINDOW) == [] and source.status.status == 'no_data'


def test_network_retry_is_bounded_and_never_retries_authorization(monkeypatch):
    calls = []
    def fetch(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise URLError('temporary TLS failure at ' + request.full_url)
        return response([entry(1)], 1)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    assert len(source.fetch(*WINDOW)) == 1 and len(calls) == 2
    calls.clear()
    def broken(request, timeout):
        calls.append(request)
        raise URLError('secret-fixture and https://private.test/?apiKey=secret-fixture')
    monkeypatch.setattr(elsevier, 'urlopen', broken)
    assert source.fetch(*WINDOW) == [] and len(calls) == 2
    assert 'secret-fixture' not in source.status.message and 'private.test' not in source.status.message


@pytest.mark.parametrize('code,state', [(401,'access_denied'), (403,'access_denied'), (429,'quota_exhausted')])
def test_manual_restrictions_visible_without_extra_requests(monkeypatch, code, state):
    calls = []
    def fetch(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, code, 'secret-fixture', {}, None)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    assert source.fetch(*WINDOW) == [] and len(calls) == 1
    assert source.status.status == state and f'HTTP {code}' in source.status.message
    assert 'secret-fixture' not in source.status.message


def test_pagination_failure_keeps_previous_rows(monkeypatch):
    def fetch(request, timeout):
        if parse_qs(urlsplit(request.full_url).query)['start'] == ['0']:
            return response([entry(1)], 100)
        raise HTTPError(request.full_url, 429, 'Limit', {}, None)
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    source = adapter()
    rows = source.fetch(*WINDOW)
    assert len(rows) == 1 and source.status.status == 'partial'
    assert '已有结果保留' in source.status.message and 'HTTP 429' in source.status.message


def test_http_200_api_error_is_not_cached_as_empty_search(monkeypatch):
    payload = {'service-error': {'status': {'statusCode': 'AUTHORIZATION_ERROR', 'statusText': 'secret-fixture'}}}
    monkeypatch.setattr(elsevier, 'urlopen', lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    source = adapter()
    assert source.fetch(*WINDOW) == [] and source.status.status == 'access_denied'
    assert 'secret-fixture' not in source.status.message
