"""Private records of searches actually performed, including session tool imports.

Imports are discovery evidence only. They cannot approve paper identities, and
are never disguised as another provider's cache or a background tool capability.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from src.models import parse_date
from src.public_sources import read, write

PRIVATE = Path('data/cache/paper-coverage/query-ledger')
PROVIDERS = {'codex_session', 'serpapi', 'sogou'}
DECISIONS = Path('data/cache/paper-coverage/discovery-decisions')


def academic_source_reason(value):
    """Skip identifiable primary papers, not ordinary publisher news or blogs."""
    try:
        parts = urlsplit(str(value))
        host, path = (parts.hostname or '').casefold(), unquote(parts.path).casefold()
    except ValueError:
        return ''
    if path.endswith('.pdf'):
        return 'primary_pdf_document'
    if host in ('doi.org', 'dx.doi.org') and path.startswith('/10.'):
        return 'doi_resolver'
    patterns = {
        'arxiv.org': r'^/(?:abs|pdf|html)/', 'export.arxiv.org': r'^/(?:abs|pdf|html)/',
        'nature.com': r'^/articles/s[0-9]', 'www.nature.com': r'^/articles/s[0-9]',
        'sciencedirect.com': r'^/science/article/', 'www.sciencedirect.com': r'^/science/article/',
        'pubmed.ncbi.nlm.nih.gov': r'^/\d+/?$', 'pmc.ncbi.nlm.nih.gov': r'^/articles/',
        'ncbi.nlm.nih.gov': r'^/pmc/articles/', 'www.ncbi.nlm.nih.gov': r'^/pmc/articles/',
        'api.crossref.org': r'^/works/', 'openalex.org': r'^/w\d+',
        'api.openalex.org': r'^/works/', 'semanticscholar.org': r'^/paper/',
        'www.semanticscholar.org': r'^/paper/', 'www.researchgate.net': r'^/publication/',
        'researchgate.net': r'^/publication/', 'link.springer.com': r'^/(?:article|chapter|book)/',
        'pubs.acs.org': r'^/doi/', 'onlinelibrary.wiley.com': r'^/doi/',
        'pubs.rsc.org': r'^/en/content/article/', 'www.biorxiv.org': r'^/content/',
        'www.medrxiv.org': r'^/content/', 'patents.google.com': r'^/patent/',
        'patents.justia.com': r'^/patent/', 'patentscope.wipo.int': r'^/search/',
        'zenodo.org': r'^/records?/', 'papers.ssrn.com': r'^/sol3/papers\.cfm$',
    }
    if host in patterns and re.search(patterns[host], path):
        return 'patent_record' if 'patent' in host else 'primary_scholarly_record'
    return ''


def admit_candidate(root, paper_id, candidate, checked_at):
    reason = academic_source_reason(candidate.get('url'))
    if not reason:
        return True
    identity = hashlib.sha256(json.dumps([paper_id, candidate['url']], ensure_ascii=False).encode()).hexdigest()
    path = Path(root) / DECISIONS / (identity + '.json')
    value = {'version': 1, 'paper_id': paper_id, 'url': candidate['url'],
             'decision': 'skip_source_read', 'reason': reason, 'checked_at': checked_at}
    if read(path) != value:
        write(path, value)
    return False


def _url(value):
    try:
        parts = urlsplit(str(value or ''))
        return bool(parts.scheme in ('http', 'https') and parts.hostname
                    and not parts.username and not parts.password
                    and parts.port in (None, 80, 443)
                    and not any(ord(c) < 33 for c in str(value)))
    except ValueError:
        return False


def _validate(row, provider, strategy, papers, now):
    if not isinstance(row, dict) or row.get('paper_id') not in papers:
        raise ValueError('Search result refers to an unknown paper')
    query = row.get('query')
    checked = parse_date(row.get('checked_at'))
    if (not isinstance(query, str) or not query.strip() or len(query) > 10000
            or any(ord(c) < 32 for c in query)
            or not checked or 'T' not in str(row.get('checked_at'))
            or checked > now + timedelta(minutes=5)
            or row.get('state') not in ('ok', 'blocked', 'error')
            or not isinstance(row.get('results'), list)):
        raise ValueError('Search result needs an actual query, time, outcome and results')
    results = []
    for result in row['results']:
        if not isinstance(result, dict) or not _url(result.get('url')):
            raise ValueError('Search result has an invalid public URL')
        value = {'url': result['url']}
        for field in ('title', 'snippet'):
            if field in result:
                if not isinstance(result[field], str):
                    raise ValueError('Search title and snippet must be text')
                value[field] = result[field]
        if value not in results:
            results.append(value)
    return {'version': 1, 'provider': provider, 'search_strategy_version': strategy,
            'paper_id': row['paper_id'], 'query': query, 'checked_at': checked.isoformat(),
            'state': row['state'], 'results': results}


def record_query(root, record):
    """Keep attempts, with identical imports producing no additional writes."""
    identity = [record[k] for k in ('provider', 'paper_id', 'search_strategy_version', 'query')]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    path = Path(root) / PRIVATE / (digest + '.json')
    saved = read(path)
    attempts = saved.get('attempts', [])
    if record in attempts:
        return False
    attempts.append(record)
    attempts.sort(key=lambda row: parse_date(row['checked_at']))
    write(path, {'version': 1, 'attempts': attempts})
    return True


def import_results(root, payload, papers, strategy, now=None):
    """Validate the entire package before any write; return newly observed rows."""
    now = now or datetime.now(timezone.utc)
    if (not isinstance(payload, dict) or payload.get('version') != 1
            or payload.get('provider') != 'codex_session'
            or type(payload.get('search_strategy_version')) is not int
            or payload['search_strategy_version'] != strategy
            or not isinstance(payload.get('queries'), list)):
        raise ValueError('Unsupported session search results package or strategy')
    records = [_validate(row, 'codex_session', strategy, set(papers), now) for row in payload['queries']]
    return [row for row in records if record_query(root, row)]


def load_queries(root, *, provider='codex_session', strategy=None):
    """Latest actual outcome per query, grouped by paper; raw text stays private."""
    result = {}
    for path in sorted((Path(root) / PRIVATE).glob('*.json')):
        attempts = read(path).get('attempts', [])
        rows = [row for row in attempts if isinstance(row, dict)
                and row.get('provider') == provider and parse_date(row.get('checked_at'))
                and (strategy is None or row.get('search_strategy_version') == strategy)]
        if rows:
            latest = dict(max(rows, key=lambda row: parse_date(row['checked_at'])))
            # A later failed/empty query does not erase its earlier source
            # discoveries. Its outcome and counts still describe that attempt.
            discoveries = {}
            for row in sorted(rows, key=lambda row: parse_date(row['checked_at'])):
                for candidate in row.get('results', []):
                    discoveries[candidate['url']] = candidate
            latest['discovered_results'] = list(discoveries.values())
            result.setdefault(latest['paper_id'], []).append(latest)
    return result


def progress(rows):
    states = {row['state'] for row in rows}
    return {'state': 'ok' if states == {'ok'} else 'partial' if 'ok' in states
            else 'blocked' if 'blocked' in states else 'error',
            'checked_at': max((row['checked_at'] for row in rows), key=parse_date),
            'queries_completed': sum(row['state'] == 'ok' for row in rows),
            'results': sum(len(row['results']) for row in rows)}
