"""On-demand retrieval and candidate ordering, isolated from daily recommendations."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode

from src.catalog import SOURCE_CATALOG
from src.models import RawRecord, SourceStatus, in_date_window
from src.settings import load_env
from src.search_ranking import SORT_LABELS, order_candidates, sort_note
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.elsevier import ElsevierAdapter
from src.sources.public_literature import (
    ArxivAdapter, CrossrefAdapter, OpenAlexAdapter, PubMedAdapter,
    SemanticScholarAdapter, WebOfScienceAdapter,
)
from src.sources.researchgate import ResearchGateIndexAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.wechat_public_index import WeChatPublicIndexAdapter

ROOT = Path(__file__).resolve().parents[1]
SOURCES = [dict(id=s['id'], label=s['label']) for s in SOURCE_CATALOG if s['kind'] == 'adapter']
MODES = {
    'CNS 子刊专项': 'Crossref · 配置中的 CNS 子刊 ISSN 专项',
    'ResearchGate': 'SerpApi 公开索引 + 本机导出；不读取登录页面',
    '微信公众号': '订阅 RSS + 本机历史文章；不足时查询公开索引，不限已订阅公众号',
    'Google Scholar': 'SerpApi Scholar · 一次查询；日期精度通常为年',
    'Elsevier': 'Scopus STANDARD · 支持相关性、日期、被引排序，日期筛选后最多读取 3 页',
    'Web of Science': 'Clarivate Starter API · 一次查询',
}
for source in SOURCES:
    source['mode'] = MODES.get(source['id'], '公开文献检索 API')


def matches(record, query):
    """AND terms, for local exports only; remote engines rank their own results."""
    text = ' '.join([record.title, record.abstract, record.venue, record.doi, *record.authors]).casefold()
    terms = re.findall(r'[\w.-]+', query.casefold())
    return all(term in text for term in terms)


def crossref_rows(payload):
    rows = CrossrefAdapter.parse_payload(payload)
    by_doi = {x.get('DOI', '').lower(): x for x in payload.get('message', {}).get('items', [])}
    for row in rows:
        item = by_doi.get(row.doi.lower(), {})
        row.raw_metadata['bibliography'] = {
            'authors': item.get('author', []), 'type': item.get('type', ''),
            'volume': item.get('volume', ''), 'issue': item.get('issue', ''),
            'page': item.get('page') or item.get('article-number', ''),
            'publisher': item.get('publisher', ''),
        }
        links = item.get('link') or []
        row.raw_metadata['pdf_link'] = next((link.get('URL', '') for link in links
                                           if link.get('content-type') == 'application/pdf'), '')
    return rows


def fetch_source(source, query, since, until, limit, cache_dir, journal_issn='', sort_by='relevance'):
    """Bounded source queries; Scopus can read up to three pages after date filtering."""
    kwargs = {'queries': [query], 'timeout': 18}
    if source in ('Crossref', 'CNS 子刊专项'):
        adapter = CrossrefAdapter(**kwargs)
        filters = [f'from-pub-date:{since.date()}', f'until-pub-date:{until.date()}']
        if journal_issn:
            from src.custom_journals import normalize_issn
            filters.append('issn:' + normalize_issn(journal_issn))
        if source == 'CNS 子刊专项':
            config = json.loads((ROOT / 'config/cns-search.json').read_text(encoding='utf-8'))
            filters.extend('issn:' + j['issn'] for j in config['journals'])
        params = {'filter': ','.join(filters), 'rows': limit}
        if query:
            params['query.bibliographic'] = query
        field = {'relevance': 'relevance' if query else 'published', 'latest': 'published',
                 'citations': 'is-referenced-by-count'}[sort_by]
        params.update(sort=field, order='desc')
        payload = adapter._get_json('https://api.crossref.org/works?' + urlencode(params))
        rows = crossref_rows(payload)
        for row in rows:
            row.source = source
        return rows, SourceStatus(source, 'ok' if rows else 'no_data', len(rows))
    if source == 'ResearchGate':
        local = ResearchGateImportAdapter()
        rows = [r for r in local.fetch(since, until) if matches(r, query)]
        index = ResearchGateIndexAdapter(timeout=18, cache_dir=cache_dir / 'scholar')
        index.queries = ['site:researchgate.net/publication ' + query]
        rows.extend(index.fetch(since, until))
        state = index.status.status
        if rows and state not in ('ok', 'no_data'):
            state = 'partial'
        elif rows:
            state = 'ok'
        return rows, SourceStatus(source, state, len(rows), MODES[source])
    if source == '微信公众号':
        adapter = WeChatRSSAdapter(timeout=6, merge_import=True)
        # Limit subscribed feed requests; no private WeChat API or login is touched.
        omitted = len(adapter.urls) > 3
        adapter.urls = adapter.urls[:3]
        rows = [r for r in adapter.fetch(since, until) if matches(r, query)]
        message = f'订阅 RSS 与本机历史文章匹配 {len(rows)} 条。'
        local_state = adapter.status.status
        state = 'ok' if rows else 'no_data'
        if len(rows) < limit:
            # Manual keywords search all indexed accounts. The daily adapter
            # still defaults to the subscription allowlist. Reuse the raw
            # query cache and shared cooldown, not the daily filtered results.
            index = WeChatPublicIndexAdapter(queries=[query], timeout=18, subscribed_only=False)
            rows.extend(index.fetch(since, until))
            state = index.status.status
            if rows:
                state = 'ok' if state in ('ok', 'no_data') else 'partial'
            message += ' ' + index.status.message
        if local_state not in ('ok', 'no_data') or omitted:
            if state == 'ok':
                state = 'partial'
            message += ' 订阅数据未完整刷新，已有历史数据保留。'
        if omitted:
            message += ' 本次最多读取 3 个 RSS 地址及本机历史导出。'
        # Prefer original article links when an RSS record is also indexed.
        unique = {}
        for row in rows:
            identity = (row.venue.casefold(), row.title.casefold())
            unique.setdefault(identity, row)
        rows = list(unique.values())
        return rows, SourceStatus(source, state, len(rows), message)
    if source == 'Elsevier':
        # Wrap literal terms: user text cannot inject Scopus fields/operators.
        literal = re.sub(r'["{}()\\]', ' ', query)
        adapter = ElsevierAdapter(queries=[f'TITLE-ABS-KEY("{literal}")'], timeout=12,
                                  manual=True, limit=limit, sort_by=sort_by)
    elif source == 'Google Scholar':
        adapter = GoogleScholarAdapter(**kwargs, include_cns=False, cache_dir=cache_dir / 'scholar')
    elif source == 'arXiv':
        # The daily adapter may fall back to recent RSS. Manual search must not
        # return unrelated daily RSS when an actual query failed.
        adapter = ArxivAdapter(**kwargs)
        terms = re.findall(r'[\w.-]+', query)
        expr = ' AND '.join(f'all:"{term}"' for term in terms)
        expr += f' AND submittedDate:[{since:%Y%m%d}0000 TO {until:%Y%m%d}2359]'
        payload = adapter._get_text('https://export.arxiv.org/api/query?' + urlencode({
            'search_query': expr, 'start': 0, 'max_results': limit,
            'sortBy': 'submittedDate' if sort_by == 'latest' else 'relevance', 'sortOrder': 'descending',
        }))
        rows = adapter.parse_xml(payload)
        for row in rows:
            row.landing_url = row.landing_url.replace('http://arxiv.org/', 'https://arxiv.org/')
            row.oa_url = row.landing_url.replace('/abs/', '/pdf/')
        return rows, SourceStatus(source, 'ok' if rows else 'no_data', len(rows))
    else:
        factories = {'OpenAlex': OpenAlexAdapter, 'PubMed': PubMedAdapter,
                     'Semantic Scholar': SemanticScholarAdapter, 'Web of Science': WebOfScienceAdapter}
        adapter = factories[source](**kwargs)
        if source in ('OpenAlex', 'Semantic Scholar', 'PubMed'):
            adapter.sort_by = sort_by
    rows = adapter.fetch(since, until)
    rows = [r for r in rows if in_date_window(r.published_at, since, until)]
    return rows, adapter.status


def worker(data):
    load_env()
    source = data['source']
    try:
        order = data.get('sort_by', 'relevance')
        if order not in SORT_LABELS:
            raise ValueError('Invalid search order')
        rows, status = fetch_source(source, data['query'],
            datetime.fromisoformat(data['since']).replace(tzinfo=timezone.utc),
            datetime.fromisoformat(data['until']).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc), data['limit'], Path(data['cache_dir']),
            **({'journal_issn': data['journal_issn']} if data.get('journal_issn') else {}),
            **({'sort_by': order} if order != 'relevance' else {}))
        # Only whitelisted metadata crosses the worker boundary. No API URL,
        # headers, raw payload, cookie or exception text is exposed to the UI.
        clean = []
        for row in order_candidates(rows, order)[:data['limit']]:
            raw = row.raw_metadata
            row.raw_metadata = {k: raw[k] for k in (
                'sources', 'bibliography', 'pdf_link', 'link_kind', 'access_mode', 'abstract_kind') if k in raw}
            if source == 'Elsevier':
                row.raw_metadata['bibliography'] = {
                    'volume': raw.get('prism:volume', ''), 'issue': raw.get('prism:issueIdentifier', ''),
                    'page': raw.get('prism:pageRange') or raw.get('article-number', ''),
                    'type': 'journal-article' if raw.get('prism:aggregationType') == 'Journal' else '',
                }
            elif source == 'Web of Science':
                meta = raw.get('metadata', raw).get('source') or {}
                row.raw_metadata['bibliography'] = {
                    'volume': meta.get('volume', ''), 'issue': meta.get('issue', ''),
                    'page': meta.get('pages', {}).get('range', '') if isinstance(meta.get('pages'), dict) else '',
                }
            clean.append(asdict(row))
        diagnostic = ''
        if status.status not in ('ok', 'no_data'):
            code = re.search(r'HTTP(?: Error)?\s+(\d{3})', status.message)
            diagnostic = 'HTTP ' + code[1] if code else next((kind for kind in ('SSLError', 'URLError', 'TimeoutError', 'JSONDecodeError') if kind in status.message), '')
        # WeChat/Scopus details are locally constructed counts/status text only;
        # never forward raw HTTP exception strings or provider request URLs.
        message = status.message if source in ('微信公众号', 'Elsevier') else MODES.get(source, '公开 API 检索')
        return {'records': clean, 'state': status.status, 'message': message,
                'sort_note': sort_note(source, order), 'diagnostic': diagnostic}
    except Exception as exc:
        code = getattr(exc, 'code', None)
        state = 'quota_exhausted' if code == 429 else 'access_denied' if code in (401, 403) else 'error'
        return {'records': [], 'state': state, 'message': '该来源本次检索未完成，可稍后重试。',
                'diagnostic': f'HTTP {code}' if isinstance(code, int) else type(exc).__name__}


if __name__ == '__main__':
    print(json.dumps(worker(json.load(sys.stdin)), ensure_ascii=True))
