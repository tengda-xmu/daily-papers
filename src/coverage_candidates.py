"""Reconsider index-only discovery hints without treating them as paper evidence.

Cached snippets may retain a candidate for later body/image verification. They
never confirm a paper association. Rejections are private audit records, not a
URL blacklist: each call evaluates the current paper and discovery text again.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

from src import coverage_discovery
from src.models import parse_date
from src.public_sources import clean, read, write
from src.wechat_metadata import article_url


CACHE = Path('data/cache/paper-coverage')
REJECTED = CACHE / 'rejected'
TRIAGE_VERSION = 1


def canonical_candidate_url(value):
    """Normalize only article links and stable public-index search links."""
    value = str(value or '')
    if any(ord(character) < 32 for character in value):
        return ''
    direct = article_url(value)
    if direct:
        return direct
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ('http', 'https') or parts.hostname != 'weixin.sogou.com'
                or parts.path != '/weixin' or parts.username or parts.password
                or parts.port not in (None, 80, 443)):
            return ''
        query = dict(parse_qsl(parts.query))
        text = clean(query.get('query', ''))
        if query.get('type') != '2' or not text:
            return ''
        return 'https://weixin.sogou.com/weixin?type=2&query=' + quote(text, safe='')
    except ValueError:
        return ''


def _discovery_text(row):
    """Explicit fields only; never use the search query or cached raw bodies."""
    title = clean(row.get('title'))[:300] if isinstance(row.get('title'), str) else ''
    snippets = []
    for field in ('_discovery_text', 'snippet', 'summary'):
        if isinstance(row.get(field), str):
            value = clean(row[field])[:4000]
            if value and value not in snippets:
                snippets.append(value)
    return {'title': title, 'summary': ' '.join(snippets)[:12000]}


def build_discovery_index(root):
    """Return private, bounded text fields grouped by canonical source URL."""
    index, identities = {}, {}
    for path in sorted((Path(root) / CACHE / 'searches').glob('*.json')):
        payload = read(path)
        if not isinstance(payload, dict) or not isinstance(payload.get('rows'), list):
            continue
        for row in payload['rows']:
            if not isinstance(row, dict):
                continue
            url = canonical_candidate_url(row.get('url') or row.get('link'))
            if not url:
                continue
            text = _discovery_text(row)
            identity = (text['title'], text['summary'])
            if not any(identity) or identity in identities.setdefault(url, set()):
                continue
            identities[url].add(identity)
            index.setdefault(url, []).append(text)
    return index


def triage_candidates(root, paper, candidates, now, index):
    """Retain credible discovery hints and privately record rejected index rows.

    Verified associations, direct article URLs and body/image review candidates
    pass through unchanged. A retained index hint is still unverified; neither
    its fields nor its status are upgraded from cached snippets.
    """
    timestamp = parse_date(now)
    if timestamp is None:
        raise ValueError('Candidate triage requires a valid review time')
    kept = []
    for row in candidates:
        if not isinstance(row, dict):
            continue
        if (row.get('match_status') == 'verified' or row.get('reason') != 'index_only'
                or article_url(row.get('url'))):
            kept.append(row)
            continue
        url = canonical_candidate_url(row.get('url'))
        discovery = [_discovery_text(row)]
        for cached in index.get(url, []) if url else []:
            if isinstance(cached, dict):
                text = _discovery_text(cached)
                if text not in discovery:
                    discovery.append(text)
        if any(coverage_discovery.local_candidate(paper, text) for text in discovery):
            kept.append(row)
            continue
        identity = json.dumps([str(paper.get('id') or ''), url or str(row.get('url') or '')], ensure_ascii=False)
        name = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        evidence = json.dumps(discovery, ensure_ascii=False, sort_keys=True)
        write(Path(root) / REJECTED / (name + '.json'), {
            'version': TRIAGE_VERSION,
            'search_strategy_version': coverage_discovery.SEARCH_STRATEGY_VERSION,
            'paper_id': str(paper.get('id') or ''),
            'url': url or str(row.get('url') or ''),
            'title': clean(row.get('title'))[:300],
            'account': clean(row.get('account'))[:100],
            'reason': 'index_candidate_not_relevant',
            'rejected_at': timestamp.isoformat(),
            'discovery_version': hashlib.sha256(evidence.encode('utf-8')).hexdigest(),
            'discovery': discovery,
        })
    return kept
