import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from connectors.codex_bridge.search import SearchRequest, SearchService
from src import manual_search
from src.models import RawRecord, SourceStatus
from src.search_ranking import rank_results, sort_note
from src.sources import elsevier, public_literature

START = datetime(2021, 9, 24, tzinfo=timezone.utc)
END = datetime(2026, 9, 24, 23, 59, 59, tzinfo=timezone.utc)


def request(mode='relevance', sources=None):
    return SearchRequest(query='Kriging', sources=sources or ['Crossref'], since=START.date(),
                         until=END.date(), sort_by=mode)


def test_order_validation():
    assert request().sort_by == 'relevance'
    with pytest.raises(ValidationError):
        request('citations;secret')


@pytest.mark.parametrize('mode,field', [('relevance', 'relevance'), ('latest', 'published'),
                                     ('citations', 'is-referenced-by-count')])
def test_crossref_sends_order_with_query_dates_and_issn(monkeypatch, tmp_path, mode, field):
    seen = []
    def fetch(self, url, headers=None):
        seen.append(parse_qs(urlsplit(url).query))
        return {'message': {'items': []}}
    monkeypatch.setattr(public_literature.CrossrefAdapter, '_get_json', fetch)
    manual_search.fetch_source('Crossref', 'Kriging', START, END, 10, tmp_path,
                               journal_issn='0028-0836', sort_by=mode)
    assert seen[0]['sort'] == [field] and seen[0]['order'] == ['desc']
    assert seen[0]['query.bibliographic'] == ['Kriging']
    assert 'issn:0028-0836' in seen[0]['filter'][0]
    assert 'until-pub-date:2026-09-24' in seen[0]['filter'][0]


@pytest.mark.parametrize('mode', ['relevance', 'latest', 'citations'])
@pytest.mark.parametrize('source,cls,fields', [
    ('OpenAlex', public_literature.OpenAlexAdapter,
     ['relevance_score:desc', 'publication_date:desc', 'cited_by_count:desc']),
    ('Semantic Scholar', public_literature.SemanticScholarAdapter,
     [None, 'publicationDate:desc', 'citationCount:desc']),
    ('PubMed', public_literature.PubMedAdapter, ['relevance', 'pub_date', 'relevance']),
])
def test_native_api_ordering(monkeypatch, tmp_path, source, cls, fields, mode):
    seen = []
    def fetch(self, url, headers=None):
        seen.append(url)
        return {}
    monkeypatch.setattr(cls, '_get_json', fetch)
    monkeypatch.setattr(public_literature.time, 'sleep', lambda _: None)
    _, status = manual_search.fetch_source(source, 'Kriging', START, END, 10, tmp_path, sort_by=mode)
    assert status.status == 'no_data' and len(seen) == 1
    params = parse_qs(urlsplit(seen[0]).query)
    expected = fields[['relevance', 'latest', 'citations'].index(mode)]
    assert params.get('sort') == ([expected] if expected else None)
    assert (params.get('query') or params.get('search') or params.get('term')) == [
        '(Kriging)' if source == 'PubMed' else 'Kriging']
    if source == 'Semantic Scholar':
        assert ('/search/bulk?' in seen[0]) == (mode != 'relevance')
        assert params['publicationDateOrYear'] == ['2021-09-24:2026-09-24']


@pytest.mark.parametrize('mode,expected', [('relevance', 'relevance'), ('latest', 'submittedDate'),
                                         ('citations', 'relevance')])
def test_arxiv_has_date_order_but_no_fabricated_citations(monkeypatch, tmp_path, mode, expected):
    seen = []
    def fetch(self, url):
        seen.append(parse_qs(urlsplit(url).query))
        return '<feed xmlns="http://www.w3.org/2005/Atom"/>'
    monkeypatch.setattr(public_literature.ArxivAdapter, '_get_text', fetch)
    manual_search.fetch_source('arXiv', 'Kriging', START, END, 10, tmp_path, sort_by=mode)
    assert seen[0]['sortBy'] == [expected]
    assert '202109240000 TO 202609242359' in seen[0]['search_query'][0]
    assert '未提供被引次数' in sort_note('arXiv', 'citations')


def test_fallback_orders_candidates_before_limiting(monkeypatch, tmp_path):
    def fetch(source, query, since, until, limit, path, **kwargs):
        assert kwargs['sort_by'] == 'citations'
        return [RawRecord(source, str(n), f'Kriging {n}', citation_count=n)
                for n in range(12)], SourceStatus(source, 'ok')
    monkeypatch.setattr(manual_search, 'fetch_source', fetch)
    data = manual_search.worker(request('citations').model_dump(mode='json') |
                                {'source': 'Google Scholar', 'limit': 5, 'cache_dir': str(tmp_path)})
    assert [r['citation_count'] for r in data['records']] == [11, 10, 9, 8, 7]
    assert '候选内' in data['sort_note']


def test_relevance_uses_native_rank_only_for_relevance_retrieval():
    def rows():
        return [{'id': identifier, 'title': title, 'published_at': '',
                 '_search_observations': [{'rank': rank, 'source': 'Crossref', 'citation_count': None}]}
                for identifier, title, rank in [('a', 'Other topic', 1), ('b', 'Kriging model', 2)]]
    ranked = rank_results(rows(), 'Kriging', 'relevance')
    assert ranked[0]['id'] == 'b'  # Title match can outrank a weak upstream hit.
    assert ranked[0]['relevance_score'] > .02  # Native rank is included.
    ranked = rank_results(rows(), 'Kriging', 'citations')
    assert ranked[0]['id'] == 'b' and ranked[0]['relevance_score'] == .02


def test_dedup_count_provenance_sort_cache_and_restoration(tmp_path):
    async def run():
        calls = []
        async def fake(source, query):
            calls.append((source, query.sort_by))
            rows = [RawRecord(source, 'shared', 'Kriging shared paper', doi='10.1234/shared',
                              published_at='2023-01-01', citation_count=70 if source == 'Crossref' else 90)]
            if source == 'Crossref':
                rows += [RawRecord(source, 'zero', 'Kriging recent paper', doi='10.1234/zero',
                                   published_at='2026-09-01', citation_count=0),
                         RawRecord(source, 'missing', 'Kriging undated', doi='10.1234/missing')]
            return {'state': 'ok', 'records': [asdict(r) for r in rows]}
        service = SearchService(tmp_path, tmp_path, fake)
        for mode in ['relevance', 'latest', 'citations']:
            for _ in range(2):
                identifier = service.start(request(mode, ['Crossref', 'OpenAlex']))
                await service.get(identifier)['task']
                data = service.snapshot(identifier)
                assert data == service.snapshot(identifier)  # Ranking does not mutate stored results.
                assert len(data['records']) == 3 and data['request']['sort_by'] == mode
                first = data['records'][0]
                assert first['doi'] == ('10.1234/zero' if mode == 'latest' else '10.1234/shared')
                shared = next(r for r in data['records'] if r['doi'] == '10.1234/shared')
                assert shared['citation_count'] == 90  # Never add 70 + 90.
                assert shared['citation_counts'] == [{'source': 'OpenAlex', 'count': 90},
                                                    {'source': 'Crossref', 'count': 70}]
                assert data['records'][-1]['citation_count'] is None
                assert all(set(r['ranks']) == {'relevance', 'latest', 'citations'} for r in data['records'])
        assert len(calls) == 6  # Three distinct retrievals, no repeat calls for same order.
        restored = SearchService(tmp_path, tmp_path, fake)
        identifier = restored.start(request('citations', ['Crossref', 'OpenAlex']))
        await restored.get(identifier)['task']
        assert all(s['cached'] for s in restored.snapshot(identifier)['sources']) and len(calls) == 6
        await restored.close()
        await service.close()
    asyncio.run(run())


@pytest.mark.parametrize('mode,field', [('latest', '-coverDate'), ('citations', '-citedby-count')])
def test_scopus_native_sort_and_bounded_future_date_fallback(monkeypatch, mode, field):
    seen = []
    def fetch(req, timeout):
        params = parse_qs(urlsplit(req.full_url).query)
        seen.append(params)
        future = mode == 'latest' and len(seen) < 3
        date = '2026-12-01' if future else '2026-09-20'
        entries = [{'dc:title': f'Kriging {n}', 'dc:identifier': str(n), 'prism:doi': f'10.1234/{n}',
                    'prism:coverDate': date, 'citedby-count': str(100-n)} for n in range(25)]
        return io.BytesIO(json.dumps({'search-results': {'opensearch:totalResults': '1000', 'entry': entries}}).encode())
    monkeypatch.setattr(elsevier, 'urlopen', fetch)
    adapter = elsevier.ElsevierAdapter(api_key='fixture', queries=['Kriging'], manual=True, limit=10, sort_by=mode)
    rows = adapter.fetch(START, END)
    assert len(rows) == 10 and seen[0]['sort'] == [field]
    assert all(r.published_at == '2026-09-20' for r in rows)
    if mode == 'latest':
        assert len(seen) == 3 and seen[-1]['sort'] == ['relevancy'] and seen[-1]['start'] == ['0']
        assert adapter.status.status == 'partial' and '不是全库最新排名' in adapter.status.message
    else:
        assert len(seen) == 1 and adapter.status.status == 'ok'
        assert [r.citation_count for r in rows] == list(range(100, 90, -1))
