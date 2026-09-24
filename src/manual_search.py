"""On-demand searches. No ranking, publishing, or daily connector mutations."""
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
    '微信公众号': '已订阅公众号 RSS / 本机同步文章，缺结果时查询公开索引（共享每日限额）',
    'Google Scholar': 'SerpApi Scholar · 一次查询；日期精度通常为年',
    'Elsevier': 'Scopus API · 按当前机构授权返回元数据',
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


def fetch_source(source, query, since, until, limit, cache_dir):
    """One bounded query per source (plus Scopus authorization fallback / PubMed fetch)."""
    kwargs = {'queries': [query], 'timeout': 18}
    if source in ('Crossref', 'CNS 子刊专项'):
        adapter = CrossrefAdapter(**kwargs)
        filters = [f'from-pub-date:{since.date()}', f'until-pub-date:{until.date()}']
        if source == 'CNS 子刊专项':
            config = json.loads((ROOT / 'config/cns-search.json').read_text(encoding='utf-8'))
            filters.extend('issn:' + j['issn'] for j in config['journals'])
        payload = adapter._get_json('https://api.crossref.org/works?' + urlencode({
            'query.bibliographic': query, 'filter': ','.join(filters), 'rows': limit,
        }))
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
        adapter = WeChatRSSAdapter(timeout=12)
        # Limit subscribed feed requests; no private WeChat API or login is touched.
        omitted = len(adapter.urls) > 3
        adapter.urls = adapter.urls[:3]
        rows = [r for r in adapter.fetch(since, until) if matches(r, query)]
        state = adapter.status.status
        if state in ('ok', 'no_data'):
            state = 'ok' if rows else 'no_data'
        if omitted:
            state = 'partial'
        if not rows:
            index = WeChatPublicIndexAdapter(queries=[query], timeout=18)
            rows = index.fetch(since, until)
            state = index.status.status
        return rows, SourceStatus(source, state, len(rows), MODES[source] +
                                 ('；本次读取前 3 个 RSS 地址及本机导出' if omitted else ''))
    if source == 'Elsevier':
        # Wrap literal terms: user text cannot inject Scopus fields/operators.
        literal = re.sub(r'["{}()\\]', ' ', query)
        adapter = ElsevierAdapter(queries=[f'TITLE-ABS-KEY("{literal}")'], timeout=18)
        adapter.queries = adapter.queries[:1]
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
            'search_query': expr, 'start': 0, 'max_results': limit, 'sortBy': 'relevance',
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
    rows = adapter.fetch(since, until)
    rows = [r for r in rows if in_date_window(r.published_at, since, until)]
    return rows, adapter.status


def worker(data):
    load_env()
    source = data['source']
    try:
        rows, status = fetch_source(source, data['query'],
            datetime.fromisoformat(data['since']).replace(tzinfo=timezone.utc),
            datetime.fromisoformat(data['until']).replace(hour=23, minute=59, second=59, tzinfo=timezone.utc), data['limit'], Path(data['cache_dir']))
        # Only whitelisted metadata crosses the worker boundary. No API URL,
        # headers, raw payload, cookie or exception text is exposed to the UI.
        clean = []
        for row in rows[:data['limit']]:
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
        return {'records': clean, 'state': status.status, 'message': MODES.get(source, '公开 API 检索'), 'diagnostic': diagnostic}
    except Exception as exc:
        code = getattr(exc, 'code', None)
        state = 'quota_exhausted' if code == 429 else 'access_denied' if code in (401, 403) else 'error'
        return {'records': [], 'state': state, 'message': '该来源本次检索未完成，可稍后重试。',
                'diagnostic': f'HTTP {code}' if isinstance(code, int) else type(exc).__name__}


if __name__ == '__main__':
    print(json.dumps(worker(json.load(sys.stdin)), ensure_ascii=True))
