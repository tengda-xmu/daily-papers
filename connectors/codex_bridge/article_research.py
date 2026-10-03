"""Bounded public literature lookup, independent of recommendation collection."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from src.editions import read, write
from src.paper_sources import clean_abstract, normalized_title


def related_literature(paper, queries, directory, *, search=None, now=None):
    from src.manual_search import fetch_source
    now = now or datetime.now(timezone.utc)
    queries = list(dict.fromkeys(str(q).strip()[:180] for q in queries if str(q).strip()))[:3]
    if not queries:
        queries = [paper['title'][:180]]
    cache_key = hashlib.sha256(json.dumps([paper['id'], queries], ensure_ascii=False).encode()).hexdigest()
    path = directory / (cache_key + '.json')
    cached = read(path)
    if cached.get('expires_at', 0) > now.timestamp():
        return cached
    search = search or fetch_source
    rows, calls, failures = {}, 0, 0
    sources = ['OpenAlex', 'Crossref']
    for query in queries:
        for source in sources:
            calls += 1
            try:
                found, status = search(source, query, now - timedelta(days=365 * 5), now, 20, directory,
                                       sort_by='relevance')
                if status.status not in ('ok', 'no_data'):
                    failures += 1
                for row in found[:20]:
                    title = normalized_title(row.title)
                    if not title or title == normalized_title(paper['title']) or (row.doi and row.doi == paper.get('doi')):
                        continue
                    abstract = clean_abstract(row.abstract)
                    if not abstract:
                        continue  # A title/search teaser is not evidence about methods.
                    url = 'https://doi.org/' + row.doi if row.doi else row.landing_url
                    from src.reading_article import public_url
                    public_url(url)
                    year = int(str(row.published_at)[:4])
                    if year > now.year:
                        continue
                    rows.setdefault(row.doi or title, {'title': row.title, 'url': url, 'year': year,
                        'basis': 'abstract', 'abstract': abstract[:12000],
                        'preprint': 'arxiv' in url.casefold(), 'source': source})
            except Exception:
                failures += 1
    # At most six network searches. Use arXiv as a fallback within that budget.
    if not rows and calls < 6:
        try:
            found, status = search('arXiv', queries[0], now - timedelta(days=365 * 5), now, 20, directory, sort_by='relevance')
            calls += 1
            for row in found:
                abstract = clean_abstract(row.abstract)
                if abstract and normalized_title(row.title) != normalized_title(paper['title']):
                    from src.reading_article import public_url
                    rows.setdefault(row.doi or normalized_title(row.title), {'title': row.title,
                        'url': public_url(row.landing_url), 'year': int(str(row.published_at)[:4]),
                        'basis': 'abstract', 'abstract': abstract[:12000], 'preprint': True, 'source': 'arXiv'})
        except Exception:
            failures += 1
    # Existing, verified records survive an outage; they retain their evidence.
    if not rows and failures and cached.get('papers'):
        rows = {r['url']: r for r in cached['papers']}
    result = {'status': 'partial' if failures and rows else 'unavailable' if failures else 'ok',
              'checked_at': now.isoformat(), 'papers': list(rows.values())[:20], 'queries': queries,
              'requests': calls, 'expires_at': now.timestamp() + (86400 if failures else 7 * 86400)}
    write(path, result)
    return result
