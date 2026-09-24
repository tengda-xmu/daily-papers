"""One provider page per interactive request; continuations stay on the server.

Daily collection keeps its separate budgets. These requests never follow a
provider-supplied URL, and never truncate a fetched page before saving it.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

from src.models import SourceStatus, in_date_window
from src.manual_search import ROOT, crossref_rows, matches
from src.sources.public_literature import (
    PublicLiteratureAdapter, CrossrefAdapter, OpenAlexAdapter, ArxivAdapter,
    SemanticScholarAdapter, PubMedAdapter, WebOfScienceAdapter,
)
from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter, _in_window, _error_status
from src.sources.researchgate import ResearchGateIndexAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.wechat_public_index import WeChatPublicIndexAdapter

BATCH_SIZE = 25


def offset_next(offset, count, total, size=BATCH_SIZE):
    if not count:
        return None
    more = offset + count < int(total) if total is not None else count >= size
    return {'offset': offset + count} if more else None


def fetch_page(source, query, since, until, cache_dir, journal_issn='', sort_by='relevance', continuation=None):
    cursor = continuation or {}
    offset = int(cursor.get('offset', 0))
    adapter = PublicLiteratureAdapter(queries=[query], timeout=18)
    message = ''
    if source in ('Crossref', 'CNS 子刊专项'):
        filters = [f'from-pub-date:{since.date()}', f'until-pub-date:{until.date()}']
        if journal_issn:
            filters.append('issn:' + journal_issn)
        if source == 'CNS 子刊专项':
            config = json.loads((ROOT / 'config/cns-search.json').read_text(encoding='utf-8'))
            filters.extend('issn:' + j['issn'] for j in config['journals'])
        params = {'filter': ','.join(filters), 'rows': BATCH_SIZE, 'cursor': cursor.get('cursor', '*'),
                  'sort': {'relevance': 'relevance' if query else 'published', 'latest': 'published',
                           'citations': 'is-referenced-by-count'}[sort_by], 'order': 'desc'}
        if query:
            params['query.bibliographic'] = query
        payload = CrossrefAdapter(queries=[query], timeout=18)._get_json('https://api.crossref.org/works?' + urlencode(params))
        body = payload['message']
        rows = crossref_rows(payload)
        for row in rows:
            row.source = source
        next_page = {'cursor': body['next-cursor']} if body.get('next-cursor') and len(body['items']) >= BATCH_SIZE else None
    elif source == 'OpenAlex':
        params = {'search': query, 'filter': f'from_publication_date:{since.date()},to_publication_date:{until.date()}',
                  'per-page': BATCH_SIZE, 'cursor': cursor.get('cursor', '*'),
                  'sort': {'relevance': 'relevance_score:desc', 'latest': 'publication_date:desc', 'citations': 'cited_by_count:desc'}[sort_by]}
        headers = {'Authorization': 'Bearer ' + os.environ['OPENALEX_API_KEY']} if os.getenv('OPENALEX_API_KEY') else {}
        payload = adapter._get_json('https://api.openalex.org/works?' + urlencode(params), headers)
        rows = OpenAlexAdapter.parse_payload(payload)
        following = (payload.get('meta') or {}).get('next_cursor')
        next_page = {'cursor': following} if following and payload.get('results') else None
    elif source == 'Semantic Scholar':
        params = {'query': query, 'publicationDateOrYear': f'{since.date()}:{until.date()}',
                  'fields': 'title,authors,abstract,year,publicationDate,venue,externalIds,url,openAccessPdf,citationCount'}
        endpoint = 'https://api.semanticscholar.org/graph/v1/paper/search'
        if sort_by == 'relevance':
            if offset >= 1000:
                return [], SourceStatus(source, 'access_denied', 0, '该来源相关性接口仅开放前 1000 条；可缩小日期范围，或使用最新、热度排序继续检索。'), cursor
            params.update(offset=offset, limit=BATCH_SIZE)
        else:
            endpoint += '/bulk'
            params['sort'] = 'citationCount:desc' if sort_by == 'citations' else 'publicationDate:desc'
            if cursor.get('token'):
                params['token'] = cursor['token']
        headers = {'x-api-key': os.environ['SEMANTIC_SCHOLAR_API_KEY']} if os.getenv('SEMANTIC_SCHOLAR_API_KEY') else {}
        payload = adapter._get_json(endpoint + '?' + urlencode(params), headers)
        rows = SemanticScholarAdapter.parse_payload(payload)
        if sort_by == 'relevance':
            next_page = {'offset': payload['next']} if payload.get('next') is not None else None
        else:
            next_page = {'token': payload['token']} if payload.get('token') else None
    elif source == 'arXiv':
        expr = ' AND '.join(f'all:"{term}"' for term in re.findall(r'[\w.-]+', query))
        expr += f' AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]'
        xml = adapter._get_text('https://export.arxiv.org/api/query?' + urlencode({
            'search_query': expr, 'start': offset, 'max_results': BATCH_SIZE,
            'sortBy': 'submittedDate' if sort_by == 'latest' else 'relevance', 'sortOrder': 'descending'}))
        rows = ArxivAdapter.parse_xml(xml)
        total = ET.fromstring(xml).findtext('{http://a9.com/-/spec/opensearch/1.1/}totalResults')
        next_page = offset_next(offset, len(rows), total)
        for row in rows:
            row.landing_url = row.landing_url.replace('http://arxiv.org/', 'https://arxiv.org/')
            row.oa_url = row.landing_url.replace('/abs/', '/pdf/')
    elif source == 'PubMed':
        if offset >= 10000:
            return [], SourceStatus(source, 'access_denied', 0, 'PubMed ESearch 仅开放前 10000 条；请缩小日期范围后继续检索。'), cursor
        payload = adapter._get_json('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?' + urlencode({
            'db': 'pubmed', 'term': f'({query})', 'retmode': 'json', 'retmax': BATCH_SIZE, 'retstart': offset,
            'mindate': since.strftime('%Y/%m/%d'), 'maxdate': until.strftime('%Y/%m/%d'),
            'datetype': 'pdat', 'sort': 'pub_date' if sort_by == 'latest' else 'relevance'}))
        found = payload['esearchresult']; ids = found.get('idlist', [])
        next_page = offset_next(offset, len(ids), found.get('count'))
        rows = PubMedAdapter.parse_xml(adapter._get_text('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?' + urlencode({
            'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'}))) if ids else []
    elif source == 'Web of Science':
        key = os.getenv('WOS_API_KEY', '').strip()
        if not key:
            return [], SourceStatus(source, 'authorization_required'), cursor
        page = int(cursor.get('page', 1))
        payload = adapter._get_json('https://api.clarivate.com/apis/wos-starter/v1/documents?' + urlencode({
            'db': 'WOS', 'q': f'TS=({query})', 'limit': BATCH_SIZE, 'page': page,
            'sortField': 'LD+D', 'publishTimeSpan': f'{since:%Y-%m-%d}+{until:%Y-%m-%d}'}), {'X-ApiKey': key})
        hits = payload['hits']
        rows = WebOfScienceAdapter.parse_payload(payload)
        total = (payload.get('metadata') or {}).get('total')
        more = offset_next((page - 1) * BATCH_SIZE, len(hits), total)
        next_page = {'page': page + 1} if more else None
    elif source == 'Elsevier':
        scopus = ElsevierAdapter(manual=True)
        if not scopus.api_key:
            return [], SourceStatus(source, 'configuration_missing'), cursor
        literal = re.sub(r'["{}()\\]', ' ', query)
        params = {'query': f'TITLE-ABS-KEY("{literal}")', 'count': BATCH_SIZE, 'view': 'STANDARD',
                  'date': f'{since.year}-{until.year}', 'cursor': cursor.get('cursor', '*'),
                  'sort': {'relevance': 'relevancy', 'latest': '-coverDate', 'citations': '-citedby-count'}[sort_by]}
        payload = adapter._get_json('https://api.elsevier.com/content/search/scopus?' + urlencode(params), scopus._headers())
        error = payload.get('service-error', {}).get('status', {})
        if error:
            code = {'AUTHORIZATION_ERROR': 403, 'AUTHENTICATION_ERROR': 401, 'QUOTA_EXCEEDED': 429}.get(error.get('statusCode'), 502)
            raise HTTPError('https://api.elsevier.com/content/search/scopus', code, 'Scopus API error', {}, None)
        body = payload['search-results']
        if any(item.get('error') and item['error'] != 'RESULT_NOT_FOUND' for item in body.get('entry', [])):
            raise ValueError('Invalid Scopus entries')
        rows = ElsevierAdapter.parse_payload(payload)
        following = (body.get('cursor') or {}).get('@next')
        next_page = {'cursor': following} if rows and following and following != params['cursor'] else None
        if rows and not body.get('cursor') and int(body.get('opensearch:totalResults', 0)) > len(rows):
            return rows, SourceStatus(source, 'partial', len(rows), 'Scopus 未提供后续游标，请检查账号的分页权限。'), None
        message = 'Scopus STANDARD 按所选排序逐页检索；日期以期刊日期为准。'
    elif source in ('Google Scholar', 'ResearchGate'):
        return scholar_page(source, query, since, until, cache_dir, cursor)
    elif source == '微信公众号':
        return wechat_page(query, since, until, cursor)
    else:
        raise ValueError('Unknown search source')
    rows = [row for row in rows if in_date_window(row.published_at, since, until)]
    return rows, SourceStatus(source, 'ok' if rows or next_page else 'no_data', len(rows), message), next_page


def scholar_page(source, query, since, until, cache_dir, cursor):
    offset = int(cursor.get('offset', 0))
    cls = ResearchGateIndexAdapter if source == 'ResearchGate' else GoogleScholarAdapter
    actual_query = 'site:researchgate.net/publication ' + query if source == 'ResearchGate' else query
    scholar = cls(queries=[actual_query], cache_dir=cache_dir / 'scholar', timeout=18)
    local = []
    if source == 'ResearchGate' and not cursor.get('local_done'):
        local = [row for row in ResearchGateImportAdapter().fetch(since, until) if matches(row, query)]
    if not scholar.api_key:
        return local, SourceStatus(source, 'partial' if local else 'configuration_missing', len(local),
                                   '公开索引需要 SerpApi 授权；本机导出结果已保留。'), cursor
    try:
        key = f'paged-v1|{since.year}-{until.year}|{actual_query}|{offset}'
        payload = scholar._read_cache(key)
        if payload is None:
            payload = PublicLiteratureAdapter(timeout=18)._get_json('https://serpapi.com/search.json?' + urlencode({
                'engine': 'google_scholar', 'q': actual_query, 'api_key': scholar.api_key,
                'num': 20, 'start': offset, 'as_ylo': since.year, 'as_yhi': until.year}))
            from src.redaction import public_metadata
            payload = public_metadata(payload, (scholar.api_key,))
            if payload.get('search_metadata', {}).get('status') == 'Success' and payload.get('error') == "Google hasn't returned any results for this query.":
                payload.pop('error')
            if not payload.get('error'):
                scholar._write_cache(key, payload)
        if payload.get('error'):
            raise RuntimeError(payload['error'])
        rows = local + [row for row in cls.parse_payload(payload) if _in_window(row, since, until)]
        # Only extract numeric offset; never follow a URL containing an API key.
        following = (payload.get('serpapi_pagination') or {}).get('next')
        count = len(payload.get('organic_results') or [])
        next_offset = offset + count
        if following:
            value = parse_qs(urlsplit(following).query).get('start', [''])[0]
            if value.isdigit() and int(value) > offset:
                next_offset = int(value)
        next_page = {'offset': next_offset, 'local_done': True} if following and count else None
        return rows, SourceStatus(source, 'ok' if rows or next_page else 'no_data', len(rows)), next_page
    except Exception as exc:
        return local, SourceStatus(source, _error_status(exc), len(local), '公开索引本页请求失败，已有结果保留，可重试。'), cursor


def wechat_page(query, since, until, cursor):
    local, note = [], ''
    if not cursor.get('local_done'):
        rss = WeChatRSSAdapter(timeout=6, merge_import=True)
        rss.urls = rss.urls[:3]
        local = [row for row in rss.fetch(since, until) if matches(row, query)]
        note = f'本机历史与订阅文章匹配 {len(local)} 条。'
        if len(local) >= BATCH_SIZE:
            return local, SourceStatus('微信公众号', 'ok', len(local), note), {'page': 1, 'local_done': True}
    page = int(cursor.get('page', 1))
    index = WeChatPublicIndexAdapter(queries=[query], timeout=18, subscribed_only=False, page=page, manual_paging=True)
    rows = local + index.fetch(since, until)
    status = index.status
    status.count = len(rows); status.message = note + status.message
    following = {'page': page + 1, 'local_done': True} if index.has_more else None
    if status.status not in ('ok', 'no_data'):
        following = cursor
    elif rows:
        status.status = 'ok'
    return rows, status, following
