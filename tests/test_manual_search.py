import asyncio
from dataclasses import asdict
from datetime import date, datetime, timezone
import json

from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.search import SearchRequest, SearchService, SOURCES
from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN
from src.citations import reference
from src.manual_search import crossref_rows, fetch_source, worker
from src.models import RawRecord, SourceStatus


def request(sources=None, **kwargs):
    return SearchRequest(query='AgenticSciML', sources=sources or [s['id'] for s in SOURCES],
                         since=date(2021, 1, 1), until=date.today(), **kwargs)


def record(source, doi='10.1234/abc', title='A scientific model'):
    return asdict(RawRecord(source, 'one', title, ['Jane Smith'], 'A Journal',
        published_at='2025-03-01', doi=doi, landing_url='https://doi.org/' + doi))


def test_all_eleven_workers_isolated_dedup_and_cache(tmp_path):
    async def run():
        calls = []
        concurrent, peak = 0, 0
        async def fake(source, query):
            nonlocal concurrent, peak
            concurrent += 1; peak = max(peak, concurrent)
            calls.append((source, query.query))
            await asyncio.sleep(.005)
            concurrent -= 1
            if source == 'Elsevier':
                raise TimeoutError()
            return {'state': 'ok', 'records': [record(source)], 'message': 'test'}
        service = SearchService(tmp_path, tmp_path, worker=fake)
        identifier = service.start(request())
        assert service.start(request()) == identifier
        with pytest.raises(Exception):
            service.start(SearchRequest(query='another', sources=['Crossref'], since='2020-01-01', until=date.today()))
        await service.get(identifier)['task']
        data = service.snapshot(identifier)
        assert len(data['sources']) == 11 and len(data['records']) == 1
        assert peak <= 4
        assert {q for _, q in calls} == {'AgenticSciML'}
        assert data['records'][0]['sources'] == sorted(set(s['id'] for s in SOURCES) - {'Elsevier'})
        assert next(s for s in data['sources'] if s['id'] == 'Elsevier')['state'] == 'timeout'
        identifier2 = service.start(request())
        await service.get(identifier2)['task']
        assert len(calls) == 12  # Successful sources reuse cache; timed-out worker can retry.
        assert sum(s['cached'] for s in service.snapshot(identifier2)['sources']) == 10
        await service.close()
    asyncio.run(run())


def test_cancel_stops_workers_and_keeps_completed_results(tmp_path):
    async def run():
        stopped = []
        async def fake(source, query):
            if source == 'Crossref':
                return {'state': 'ok', 'records': [record(source)]}
            try:
                await asyncio.sleep(5)
            finally:
                stopped.append(source)
        service = SearchService(tmp_path, tmp_path, fake)
        identifier = service.start(request(['Crossref', 'PubMed']))
        await asyncio.sleep(.02)
        data = await service.stop(identifier)
        assert data['state'] == 'cancelled' and len(data['records']) == 1
        assert stopped == ['PubMed']
    asyncio.run(run())


def test_merged_citation_keeps_journal_year_not_preprint_year(tmp_path):
    async def run():
        async def fake(source, query):
            row = record(source)
            if source == 'Crossref':
                row['published_at'] = '2026-08-10'
                row['raw_metadata'] = {'bibliography': {'authors': [{'family': 'Smith', 'given': 'Jane'}], 'volume': '2', 'issue': '1', 'page': '57', 'type': 'journal-article'}}
            else:
                row['abstract'] = 'A long abstract makes the index the best reading record.'
                row['published_at'] = '2025-06-01'
            return {'state': 'ok', 'records': [row]}
        service = SearchService(tmp_path, tmp_path, fake)
        identifier = service.start(request(['Crossref', 'Semantic Scholar']))
        await service.get(identifier)['task']
        row = service.snapshot(identifier)['records'][0]
        assert row['published_at'] == '2026-08-10'
        assert ', 2026, 2(1): 57' in row['citation']['text']
    asyncio.run(run())


def test_auth_does_not_start_codex_and_disallows_unknown_downloads(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert client.get('/api/search/sources').status_code == 401
        pair = client.post('/api/pair', json={'code': app.state.pair_code}).json()
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + pair['token']}
        assert len(client.get('/api/search/sources', headers=headers).json()['sources']) == 11
        assert client.get('/api/search/sources', headers=headers | {'Origin': 'https://evil.example'}).status_code == 403
        assert client.get('/api/search/unknown/../../pdf', headers=headers).status_code == 404
        bad = request().model_dump(mode='json') | {'sources': ['file:///private']}
        assert client.post('/api/search', json=bad, headers=headers).status_code == 422
        assert client.get('/api/search/unknown/result/pdf', headers=headers).status_code == 404
        async def fake(source, query):
            return {'state': 'ok', 'records': [record(source)]}
        app.state.search.worker = fake
        response = client.post('/api/search', json=request(['Crossref']).model_dump(mode='json'), headers=headers)
        assert response.status_code == 200
        identifier = response.json()['id']
        client.get('/api/search/' + identifier, headers=headers)
        assert client.get(f'/api/search/{identifier}/anything/pdf', headers=headers).status_code == 404


def test_crossref_cns_uses_user_query_and_all_configured_issns(monkeypatch, tmp_path):
    from src.sources.public_literature import CrossrefAdapter
    from urllib.parse import parse_qs, urlsplit
    seen = []
    def fetch(self, url, headers=None):
        seen.append(parse_qs(urlsplit(url).query))
        return {'message': {'items': []}}
    monkeypatch.setattr(CrossrefAdapter, '_get_json', fetch)
    start = datetime(2020, 1, 1, tzinfo=timezone.utc); end = datetime.now(timezone.utc)
    fetch_source('CNS 子刊专项', 'test my actual query', start, end, 10, tmp_path)
    assert seen[0]['query.bibliographic'] == ['test my actual query']
    assert seen[0]['filter'][0].count('issn:') == 9
    assert 'from-pub-date:2020-01-01' in seen[0]['filter'][0]


@pytest.mark.parametrize('source,cls', [
    ('Elsevier', 'ElsevierAdapter'), ('Google Scholar', 'GoogleScholarAdapter'),
    ('OpenAlex', 'OpenAlexAdapter'), ('Web of Science', 'WebOfScienceAdapter'),
    ('PubMed', 'PubMedAdapter'), ('Semantic Scholar', 'SemanticScholarAdapter')])
def test_manual_query_replaces_daily_focus_queries(source, cls, monkeypatch, tmp_path):
    import src.manual_search as manual
    seen = []
    def fetch(self, since, until):
        seen.extend(self.queries)
        self._status = SourceStatus(source, 'no_data')
        return []
    monkeypatch.setattr(getattr(manual, cls), 'fetch', fetch)
    fetch_source(source, 'unrelated custom topic', datetime(2021, 1, 1, tzinfo=timezone.utc), datetime.now(timezone.utc), 10, tmp_path)
    assert len(seen) == 1
    assert 'unrelated custom topic' in seen[0]
    assert 'CNS' not in seen[0]


def test_worker_redacts_raw_metadata_and_includes_final_day(monkeypatch, tmp_path):
    import src.manual_search as manual
    dates = []
    def fetch(source, query, since, until, limit, path):
        dates.append(until)
        row = RawRecord(**record(source))
        row.raw_metadata = {'api_key': 'secret', 'headers': {'Authorization': 'secret'}, 'bibliography': {'volume': '1'}}
        return [row], SourceStatus(source, 'ok', 1, 'secret')
    monkeypatch.setattr(manual, 'fetch_source', fetch)
    data = worker(request(['Crossref']).model_dump(mode='json') | {'source': 'Crossref', 'cache_dir': str(tmp_path)})
    assert 'secret' not in json.dumps(data)
    assert dates[0].hour == 23
    assert data['records'][0]['raw_metadata']['bibliography']['volume'] == '1'


def test_wechat_manual_search_fills_from_all_accounts_and_surfaces_limits(monkeypatch, tmp_path):
    import src.manual_search as manual
    seen = []
    def local_fetch(self, since, until):
        assert self.merge_import
        self._status = SourceStatus('微信公众号', 'ok')
        return [RawRecord('微信公众号', 'local', 'Kriging local', venue='历史号',
                          landing_url='https://mp.weixin.qq.com/s/local', published_at='2026-09-20')]
    def public_fetch(self, since, until):
        seen.append(self.subscribed_only)
        assert self.queries == ['Kriging']
        self._status = SourceStatus('微信公众号', 'quota_exhausted', 0, '当日公开检索预算已用完，缓存结果保留，次日继续。')
        return []
    monkeypatch.setattr(manual.WeChatRSSAdapter, 'fetch', local_fetch)
    monkeypatch.setattr(manual.WeChatPublicIndexAdapter, 'fetch', public_fetch)
    data = worker({'query': 'Kriging', 'source': '微信公众号', 'since': '2021-09-24',
                   'until': '2026-09-24', 'limit': 10, 'cache_dir': str(tmp_path)})
    assert seen == [False] and data['state'] == 'partial'
    assert len(data['records']) == 1 and '预算已用完' in data['message']
    assert '历史文章匹配 1 条' in data['message']


def test_wechat_fixed_scope_invalidates_only_old_wechat_cache(tmp_path):
    import hashlib
    async def run():
        calls = []
        async def fake(source, query):
            calls.append(source)
            return {'state': 'ok', 'records': [record(source)], 'message': '不限已订阅公众号'}
        service = SearchService(tmp_path, tmp_path, fake)
        req = request(['微信公众号', 'Crossref'])
        # This is the old cache format which recorded false no_data for WeChat.
        for source in req.sources:
            key = hashlib.sha256(json.dumps(req.model_dump(mode='json', exclude={'sort_by'}) | {'sources': [source]}, sort_keys=True).encode()).hexdigest()
            (service.directory / (key + '.json')).write_text(json.dumps({'state': 'no_data', 'records': []}))
        identifier = service.start(req)
        await service.get(identifier)['task']
        assert calls == ['微信公众号']  # Other sources do not incur a new paid request.
        snapshot = service.snapshot(identifier)
        state = next(s for s in snapshot['sources'] if s['id'] == '微信公众号')
        assert state['detail'] == '不限已订阅公众号'
        assert state['search_url'].startswith('https://weixin.sogou.com/weixin?')
        next_id = service.start(req)
        await service.get(next_id)['task']
        assert calls == ['微信公众号']
        assert all(s['cached'] for s in service.snapshot(next_id)['sources'])
    asyncio.run(run())


def test_scopus_old_date_sorted_empty_cache_is_not_reused(tmp_path):
    import hashlib
    async def run():
        calls = []
        async def fake(source, query):
            calls.append(source)
            return {'state': 'ok', 'records': [record(source)], 'message': 'Scopus STANDARD：按相关性读取 1 页。'}
        service = SearchService(tmp_path, tmp_path, fake)
        req = request(['Elsevier', 'Google Scholar'])
        for source in req.sources:
            key = hashlib.sha256(json.dumps(req.model_dump(mode='json', exclude={'sort_by'}) | {'sources': [source]}, sort_keys=True).encode()).hexdigest()
            (service.directory / (key + '.json')).write_text(json.dumps({'state': 'no_data', 'records': []}))
        identifier = service.start(req)
        await service.get(identifier)['task']
        assert calls == ['Elsevier']
        assert len(service.snapshot(identifier)['records']) == 1
        identifier = service.start(req)
        await service.get(identifier)['task']
        assert calls == ['Elsevier']  # Fixed successful requests now reuse the new cache.
        assert all(s['cached'] for s in service.snapshot(identifier)['sources'])
    asyncio.run(run())


def test_journal_gbt_structured_names_volume_issue_pages_without_online_suffix():
    row = crossref_rows({'message': {'items': [{
        'DOI': '10.1234/xyz', 'title': ['Evidence and models'], 'container-title': ['Test Journal'],
        'published': {'date-parts': [[2025, 4, 3]]}, 'author': [
            {'given': 'Jane Alice', 'family': 'Smith'}, {'given': '明', 'family': '王'}],
        'type': 'journal-article', 'volume': '12', 'issue': '3', 'page': '101-110',
    }]}})[0]
    citation = reference(asdict(row), date(2026, 9, 24))
    assert citation['text'] == 'Smith J A, 王明. Evidence and models[J]. Test Journal, 2025, 12(3): 101-110.'
    assert citation['notes'] == []
    assert row.doi == '10.1234/xyz'  # Retain identifiers for original/PDF links.


def test_journal_citation_matches_requested_article_number_example():
    row = crossref_rows({'message': {'items': [{
        'DOI': '10.1234/example',
        'URL': 'https://example.org/paper',
        'title': ['Kriging-assisted hybrid reliability design and optimization of offshore wind turbine support structure based on a portfolio allocation strategy'],
        'container-title': ['Ocean Engineering'],
        'published': {'date-parts': [[2024, 3, 1]]},
        'author': [{'family': 'Meng', 'given': 'D'}, {'family': 'Yang', 'given': 'H'},
                   {'family': 'Yang', 'given': 'S'}, {'family': 'Another', 'given': 'Author'}],
        'type': 'journal-article', 'volume': '295', 'article-number': '116842',
    }]}})[0]
    citation = reference(asdict(row), date(2026, 9, 24))
    assert citation['text'] == ('Meng D, Yang H, Yang S, et al. Kriging-assisted hybrid reliability design '
        'and optimization of offshore wind turbine support structure based on a portfolio allocation '
        'strategy[J]. Ocean Engineering, 2024, 295: 116842.')
    assert citation['notes'] == []


def test_incomplete_journal_citation_uses_year_and_reports_missing_fields():
    paper = {'title': 'An early article', 'authors': ['Smith J'], 'venue': 'Test Journal',
             'published_at': '2025-04-03', 'doi': '10.1234/early',
             'landing_url': 'https://example.org/early',
             'raw_metadata': {'bibliography': {'type': 'journal-article'}}}
    citation = reference(paper, date(2026, 9, 24))
    assert citation['text'] == 'Smith J. An early article[J]. Test Journal, 2025.'
    assert any('页码/文章号不全' in note for note in citation['notes'])


def test_unknown_and_preprint_citations_never_invent_bibliography():
    unknown = reference({'title': 'Unknown paper', 'source': 'Google Scholar', 'venue': 'Smith - 2025 - website',
                         'landing_url': 'https://example.org/paper'}, date(2026, 9, 24))
    assert '[EB/OL]' in unknown['text'] and 'Smith - 2025' not in unknown['text']
    assert '[2026-09-24]. https://example.org/paper.' in unknown['text']
    assert '2025' not in unknown['text']
    assert len(unknown['notes']) >= 3
    preprint = reference({'title': 'A preprint', 'source': 'arXiv', 'published_at': '2025-01-02',
                          'landing_url': 'https://arxiv.org/abs/2501.00001'}, date(2026, 9, 24))
    assert '[PP/OL]' in preprint['text'] and 'arXiv (2025-01-02)' in preprint['text']
    assert '[2026-09-24]. https://arxiv.org/abs/2501.00001.' in preprint['text']
    many = reference({'title': 'A paper', 'authors': ['Smith J', 'Jones A', 'Taylor B', 'Walker C']})
    assert 'et al. A paper' in many['text'] and 'et al..' not in many['text']


def test_pdf_only_downloads_result_metadata_and_validates_cached_path(tmp_path, monkeypatch):
    import connectors.codex_bridge.search as module
    async def run():
        seen = []
        async def fake(source, query):
            return {'state': 'ok', 'records': [record(source)]}
        def pdf(paper, directory):
            seen.append(paper['doi'])
            (directory / 'test.pdf').write_bytes(b'%PDF-test')
            return {'file': 'test.pdf'}
        monkeypatch.setattr(module, 'fetch_pdf', pdf)
        service = SearchService(tmp_path, tmp_path, fake)
        identifier = service.start(request(['Crossref']))
        await service.get(identifier)['task']
        result_id = service.snapshot(identifier)['records'][0]['id']
        assert (await service.pdf(identifier, result_id)).read_bytes().startswith(b'%PDF-')
        await service.pdf(identifier, result_id)
        assert seen == ['10.1234/abc']  # Cache avoids another publisher request.
        with pytest.raises(Exception):
            await service.pdf(identifier, '../private')
        folder = service.directory / 'pdfs' / result_id
        (folder / 'document.json').write_text(json.dumps({'file': '../../../private.pdf'}))
        with pytest.raises(Exception):
            await service.pdf(identifier, result_id)
    asyncio.run(run())
