import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.search import SearchService, SearchRequest, MoreRequest
from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN
from src.models import RawRecord, SourceStatus
from src import search_pages as pages, manual_search
from src.sources import wechat_public_index as wechat

START = datetime(2021, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 9, 24, 23, 59, 59, tzinfo=timezone.utc)


def request(sources=None):
    return SearchRequest(query='Kriging', sources=sources or ['Crossref'], since=START.date(),
                         until=END.date(), pagination=True)


def row(source, n):
    return asdict(RawRecord(source, str(n), f'Kriging method {n}', doi=f'10.1234/{n}',
                           published_at='2025-06-01', citation_count=n))


def test_pages_pass_25_deduplicate_preserve_order_and_reuse_cached_pages(tmp_path):
    async def run():
        calls = []
        async def worker(source, query):
            offset = query._continuation.get('offset', 0)
            calls.append((source, offset))
            if source == 'PubMed':
                return {'state': 'ok', 'records': [row(source, 0)], 'next': None}
            # Overlap between pages simulates provider index changes.
            rows = [row(source, n) for n in range(max(0, offset - 2), min(63, offset + 25))]
            return {'state': 'ok', 'records': rows, 'next': {'offset': offset + 25} if offset + 25 < 63 else None}
        service = SearchService(tmp_path, tmp_path, worker)
        async def finish(identifier):
            await service.get(identifier)['task']
            return service.snapshot(identifier)
        identifier = service.start(request(['Crossref', 'PubMed']))
        first = await finish(identifier)
        first_ids = [r['id'] for r in first['records']]
        assert len(first_ids) == 25
        assert 'continuation' not in json.dumps(first)
        data = MoreRequest(round=first['round'])
        service.more(identifier, data)
        service.more(identifier, data)  # Duplicate click is idempotent.
        second = await finish(identifier)
        assert len(second['records']) == 50
        assert [r['id'] for r in second['records']][:25] == first_ids
        assert set(first_ids) <= service.get(identifier)['results'].keys()
        service.more(identifier, MoreRequest(round=second['round']))
        final = await finish(identifier)
        assert len(final['records']) == 63
        assert not any(s['has_more'] for s in final['sources'])
        assert calls == [('Crossref', 0), ('PubMed', 0), ('Crossref', 25), ('Crossref', 50)]
        # Repeating the same search and later pages uses exact page caches.
        second_id = service.start(request(['Crossref', 'PubMed']))
        cached = await finish(second_id)
        service.more(second_id, MoreRequest(round=cached['round']))
        await finish(second_id)
        assert len(calls) == 4
        await service.close()
    asyncio.run(run())


def test_failure_and_cancellation_resume_same_page_without_losing_records(tmp_path):
    async def run():
        calls, attempts = [], 0
        async def worker(source, query):
            nonlocal attempts
            offset = query._continuation.get('offset', 0)
            calls.append(offset)
            if offset:
                attempts += 1
                if attempts == 1:
                    raise TimeoutError()
                if attempts == 2:
                    await asyncio.sleep(5)
            return {'state': 'ok', 'records': [row(source, offset)], 'next': {'offset': 1} if not offset else None}
        service = SearchService(tmp_path, tmp_path, worker)
        identifier = service.start(request())
        await service.get(identifier)['task']
        service.more(identifier, MoreRequest(round=1))
        await service.get(identifier)['task']
        failed = service.snapshot(identifier)
        assert len(failed['records']) == 1
        assert failed['sources'][0]['can_retry'] and not failed['sources'][0]['has_more']
        service.more(identifier, MoreRequest(round=failed['round'], retry=True))
        await asyncio.sleep(.01)
        cancelled = await service.stop(identifier)
        assert len(cancelled['records']) == 1 and cancelled['sources'][0]['can_retry']
        service.more(identifier, MoreRequest(round=cancelled['round'], retry=True))
        await service.get(identifier)['task']
        assert len(service.snapshot(identifier)['records']) == 2
        assert calls == [0, 1, 1, 1]
    asyncio.run(run())


def test_more_endpoint_auth_source_validation_and_capability(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert client.post('/api/search/anything/more', json={'round': 1}).status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}).json()['token']
        headers = {'Authorization': 'Bearer ' + token}
        assert client.get('/api/search/sources', headers=headers).json()['pagination'] is True
        async def fake(source, query):
            return {'state': 'ok', 'records': [], 'next': None}
        app.state.search.worker = fake
        identifier = client.post('/api/search', headers=headers, json=request().model_dump(mode='json')).json()['id']
        assert client.post(f'/api/search/{identifier}/more', headers=headers, json={'round': 1, 'source': 'PubMed'}).status_code == 422
        assert client.post(f'/api/search/{identifier}/more', headers=headers, json={'round': 100}).status_code == 409


@pytest.mark.parametrize('source,first_payload,second_payload,key,value', [
    ('Crossref', {'message': {'items': [{'DOI': f'10.1/{i}', 'title': ['Kriging']} for i in range(25)], 'next-cursor': 'c2'}}, {'message': {'items': []}}, 'cursor', 'c2'),
    ('CNS 子刊专项', {'message': {'items': [{'DOI': f'10.1/{i}', 'title': ['Kriging']} for i in range(25)], 'next-cursor': 'c2'}}, {'message': {'items': []}}, 'cursor', 'c2'),
    ('OpenAlex', {'results': [{'id': 'x', 'title': 'Kriging'}], 'meta': {'next_cursor': 'c2'}}, {'results': [], 'meta': {}}, 'cursor', 'c2'),
    ('Semantic Scholar', {'data': [{'paperId': 'x', 'title': 'Kriging'}], 'next': 25}, {'data': []}, 'offset', '25'),
    ('Web of Science', {'hits': [{'uid': str(i), 'title': 'Kriging'} for i in range(25)], 'metadata': {'total': 30}}, {'hits': [], 'metadata': {'total': 30}}, 'page', '2'),
    ('Elsevier', {'search-results': {'entry': [{'dc:title': 'Kriging', 'dc:identifier': 'SCOPUS_ID:1', 'prism:coverDate': '2025-01-01'}], 'cursor': {'@next': 'c2'}}}, {'search-results': {'entry': [], 'cursor': {}}}, 'cursor', 'c2'),
    ('Google Scholar', {'organic_results': [{'title': 'Kriging', 'result_id': 'r1'}], 'serpapi_pagination': {'next': 'https://serpapi.com/search?start=20&api_key=PRIVATE'}}, {'organic_results': []}, 'start', '20'),
    ('ResearchGate', {'organic_results': [{'title': 'Kriging', 'result_id': 'r1', 'link': 'https://www.researchgate.net/publication/123_title'}], 'serpapi_pagination': {'next': 'https://serpapi.com/search?start=20&api_key=PRIVATE'}}, {'organic_results': []}, 'start', '20'),
])
def test_provider_continuations_preserve_query_and_never_follow_urls(monkeypatch, tmp_path, source, first_payload, second_payload, key, value):
    calls = []
    monkeypatch.setenv('WOS_API_KEY', 'fixture'); monkeypatch.setenv('ELSEVIER_API_KEY', 'fixture'); monkeypatch.setenv('SERPAPI_API_KEY', 'fixture')
    monkeypatch.setattr(pages.ResearchGateImportAdapter, 'fetch', lambda *a: [])
    def fetch(self, url, headers=None):
        assert 'PRIVATE' not in url
        calls.append(parse_qs(urlsplit(url).query))
        return first_payload if len(calls) == 1 else second_payload
    monkeypatch.setattr(pages.PublicLiteratureAdapter, '_get_json', fetch)
    _, _, following = pages.fetch_page(source, 'Kriging', START, END, tmp_path)
    assert following is not None
    _, _, following = pages.fetch_page(source, 'Kriging', START, END, tmp_path, continuation=following)
    assert following is None and calls[-1][key] == [value]
    for param in ('query', 'q', 'search', 'filter', 'date', 'sort', 'publishTimeSpan'):
        assert calls[0].get(param) == calls[1].get(param)


def test_semantic_bulk_keeps_entire_batch_and_uses_token(monkeypatch, tmp_path):
    calls = []
    def fetch(self, url, headers=None):
        calls.append(parse_qs(urlsplit(url).query))
        return {'data': [{'paperId': str(i), 'title': 'Kriging', 'year': 2025} for i in range(1000)], 'token': 'batch-two'} if len(calls) == 1 else {'data': []}
    monkeypatch.setattr(pages.PublicLiteratureAdapter, '_get_json', fetch)
    data = request(['Semantic Scholar']).model_dump(mode='json') | {'source': 'Semantic Scholar', 'sort_by': 'citations', 'cache_dir': str(tmp_path)}
    result = manual_search.worker(data)
    assert len(result['records']) == 1000 and result['next'] == {'token': 'batch-two'}
    manual_search.worker(data | {'continuation': result['next']})
    assert calls[-1]['token'] == ['batch-two'] and calls[-1]['sort'] == ['citationCount:desc']


def test_pubmed_and_arxiv_advance_offsets_before_date_filtering(monkeypatch, tmp_path):
    calls = []
    def fetch(self, url, headers=None):
        calls.append(parse_qs(urlsplit(url).query))
        return {'esearchresult': {'idlist': ['1', '2'], 'count': '3'}} if len(calls) == 1 else {'esearchresult': {'idlist': [], 'count': '3'}}
    monkeypatch.setattr(pages.PublicLiteratureAdapter, '_get_json', fetch)
    monkeypatch.setattr(pages.PublicLiteratureAdapter, '_get_text', lambda *a: '<PubmedArticleSet/>')
    _, _, following = pages.fetch_page('PubMed', 'Kriging', START, END, tmp_path)
    pages.fetch_page('PubMed', 'Kriging', START, END, tmp_path, continuation=following)
    assert calls[-1]['retstart'] == ['2']
    calls.clear()
    def xml(self, url):
        calls.append(parse_qs(urlsplit(url).query))
        return '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/"><o:totalResults>2</o:totalResults><entry><id>http://arxiv.org/abs/1</id><title>Kriging</title><published>2025-01-01</published></entry></feed>'
    monkeypatch.setattr(pages.PublicLiteratureAdapter, '_get_text', xml)
    _, _, following = pages.fetch_page('arXiv', 'Kriging', START, END, tmp_path)
    pages.fetch_page('arXiv', 'Kriging', START, END, tmp_path, continuation=following)
    assert calls[-1]['start'] == ['1']


def test_wechat_manual_pages_have_distinct_caches_and_daily_budget_stays_separate(monkeypatch, tmp_path):
    calls = []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(('<ul class="news-list"><li id="sogou_vr_x"><h3><a>Kriging ' + str(len(calls)) +
            '</a></h3><span class="all-time-y2"><a>研究号</a></span><script>document.write(timeConvert(\'1750000000\'));</script></li></ul><a id="sogou_next">下一页</a>').encode())
    monkeypatch.setattr(wechat, 'urlopen', fetch)
    for number in range(1, 5):
        adapter = wechat.WeChatPublicIndexAdapter(cache_dir=tmp_path, queries=['Kriging'], subscribed_only=False, page=number, manual_paging=True)
        assert adapter.fetch(START, END) and adapter.has_more
        assert adapter.fetch(START, END) and len(calls) == number
    assert 'page=4' in calls[-1]
    daily = wechat.WeChatPublicIndexAdapter(cache_dir=tmp_path, queries=['different'], subscribed_only=False)
    assert daily.fetch(START, END) == [] and daily.status.status == 'quota_exhausted'
