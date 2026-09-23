import io
import json
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter


def test_scopus_retries_authorized_standard_view_and_keeps_it(monkeypatch):
    adapter = ElsevierAdapter(api_key="fixture-secret", queries=['SRCTITLE(Nature)', 'SRCTITLE(Science)'])
    calls = []
    def response(request, timeout):
        params = parse_qs(urlsplit(request.full_url).query)
        calls.append(params)
        assert 'apiKey' not in params
        if params['view'] == ['COMPLETE']:
            raise HTTPError(request.full_url, 401, 'AUTHORIZATION_ERROR', {}, None)
        return io.BytesIO(json.dumps({'search-results': {'entry': [
            {'dc:title': 'LLM design', 'prism:doi': '10/test', 'prism:coverDate': '2026-09-01',
             'prism:url': 'https://api.elsevier.com/abstract?apiKey=fixture-secret'},
            {'dc:title': 'Future', 'prism:doi': '10/future', 'prism:coverDate': '2027-01-01'}
        ]}}).encode())
    monkeypatch.setattr('src.sources.elsevier.urlopen', response)
    records = adapter.fetch(datetime(2026, 8, 24), datetime(2026, 9, 23))
    assert [p['view'][0] for p in calls] == ['COMPLETE', 'STANDARD', 'STANDARD']
    assert all(p['date'] == ['2026'] for p in calls)
    assert len(records) == 1 and records[0].landing_url == 'https://doi.org/10/test'
    assert adapter.status.status == 'ok' and 'STANDARD' in adapter.status.message
    assert 'fixture-secret' not in json.dumps(records[0].to_dict())


def test_scopus_denied_standard_view_remains_an_access_error(monkeypatch):
    adapter = ElsevierAdapter(api_key='fixture-secret')
    calls = []
    def denied(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 403, 'fixture-secret', {}, None)
    monkeypatch.setattr('src.sources.elsevier.urlopen', denied)
    assert adapter.fetch(datetime(2026, 8, 24), datetime(2026, 9, 23)) == []
    assert len(calls) == 2 and adapter.status.status == 'access_denied'
    assert 'fixture-secret' not in adapter.status.message


def test_scholar_scopes_and_caches_paid_queries_without_credentials(tmp_path, monkeypatch):
    adapter = GoogleScholarAdapter(api_key='fixture-secret', cache_dir=tmp_path)
    adapter.queries = ['large language model fault diagnosis']
    calls = []
    def response(request, timeout):
        params = parse_qs(urlsplit(request.full_url).query)
        calls.append(params)
        return io.BytesIO(json.dumps({'search_parameters': {'api_key': 'fixture-secret'}, 'organic_results': [{
            'result_id': 'one', 'title': 'LLM maintenance',
            'link': 'https://example.org/paper?api_key=fixture-secret',
            'publication_info': {'summary': 'Author - Journal, 2026'},
            'api_key': 'fixture-secret'
        }]}).encode())
    monkeypatch.setattr('src.sources.google_scholar.urlopen', response)
    dates = (datetime(2026, 8, 24), datetime(2026, 9, 23))
    records = adapter.fetch(*dates)
    assert len(records) == 1 and adapter.status.status == 'ok'
    assert calls[0]['as_ylo'] == calls[0]['as_yhi'] == ['2026']
    assert 'fixture-secret' not in json.dumps(records[0].to_dict())
    assert all('fixture-secret' not in p.read_text() for p in tmp_path.glob('*.json'))
    adapter.fetch(*dates)
    assert len(calls) == 1
    adapter.fetch(datetime(2027, 1, 1), datetime(2027, 2, 1))
    assert len(calls) == 2  # Year bounds are part of the cache identity.


def test_scholar_failure_never_exports_credential_bearing_url(tmp_path, monkeypatch):
    adapter = GoogleScholarAdapter(api_key='fixture-secret', cache_dir=tmp_path)
    def fail(request, timeout):
        raise URLError('Failed request: ' + request.full_url)
    monkeypatch.setattr('src.sources.google_scholar.urlopen', fail)
    assert adapter.fetch(datetime(2026, 8, 24), datetime(2026, 9, 23)) == []
    assert adapter.status.status == 'error'
    assert 'fixture-secret' not in json.dumps(adapter.status.to_dict())
