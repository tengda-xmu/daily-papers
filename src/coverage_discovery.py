"""Candidate discovery for paper coverage; a candidate is never identity evidence.

The public Sogou index is independent of the subscription collector and has no
daily or per-paper quotas. Redirects are followed only on explicit public hosts;
verification pages are recorded as blocked, never executed or bypassed.
"""
from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup

from src.models import normalize_doi
from src.public_sources import clean
from src.sources.wechat_public_index import parse_search_page, PublicSearchChallenge
from src.wechat_metadata import article_url, public_index_url

SEARCH_STRATEGY_VERSION = 3
HOSTS = {'weixin.sogou.com', 'www.sogou.com', 'mp.weixin.qq.com'}
MAX_BYTES = 4_000_000
GENERIC_METHODS = {'DOI', 'PDF', 'HTML', 'HTTP', 'HTTPS', 'AI', 'ML', 'LLM', 'CNS', 'RMSE', 'MAPE',
                   'PMID', 'ISSN', 'ISBN', 'FIG', 'FIGURE', 'TABLE', 'ABSTRACT', 'REFERENCES',
                   'BIBLIOGRAPHY', 'INTRODUCTION', 'METHODS', 'RESULTS', 'DISCUSSION', 'CONCLUSION',
                   'CONCLUSIONS', 'ACKNOWLEDGMENTS', 'ACKNOWLEDGEMENTS', 'SUPPLEMENTARY'}


def _compact(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value or '')).casefold() if c.isalnum())


def _text(paper):
    fields = ('title_zh', 'summary', 'abstract', 'reading_summary', 'methods', 'method_names')
    values = []
    for field in fields:
        value = paper.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(v for v in value if isinstance(v, str))
    reading = paper.get('reading')
    if isinstance(reading, dict):
        values.extend(reading[k] for k in ('summary', 'methods') if isinstance(reading.get(k), str))
    return ' '.join(values)


def method_names(paper):
    """Use existing reading names, not invented translations or model calls."""
    explicit = paper.get('method_names') or paper.get('methods') or []
    explicit = [explicit] if isinstance(explicit, str) else explicit
    names = [clean(n) for n in explicit if isinstance(n, str) and 3 <= len(_compact(n)) <= 60
             and clean(n).upper() not in GENERIC_METHODS] if isinstance(explicit, list) else []
    for token in re.findall(r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*', _text(paper)):
        if token.upper() in GENERIC_METHODS or len(token) < 4 or re.fullmatch(r'P\d+', token):
            continue
        # Named model versions and mixed-case names are useful discovery hints.
        if (re.search(r'[A-Za-z]\d|\d[A-Za-z]', token) or re.fullmatch(r'[A-Z]{4,12}', token)
                or (re.search('[a-z]', token) and re.search('[a-z][A-Z]', token))):
            names.append(token)
    return list(dict.fromkeys(names))


def paper_queries(paper):
    """Keep DOI/English first, then Chinese and existing method-name strategies."""
    prefix = 'site:mp.weixin.qq.com '
    result = []
    doi = normalize_doi(paper.get('doi'))
    title = clean(paper.get('title')).rstrip('.')
    normalized = unicodedata.normalize('NFKC', title).translate(str.maketrans({
        '‘': "'", '’': "'", '“': '"', '”': '"', '\u00ad': None,
    }))
    normalized = re.sub(r'[\u2010-\u2015\u2212]', '-', normalized)
    if doi:
        result.append(prefix + '"' + doi + '"')
    if title:
        result.append(prefix + '"' + title.replace('"', '') + '"')
        if normalized and normalized != title:
            result.append(prefix + '"' + normalized.replace('"', '') + '"')
        result.append(prefix + normalized.replace('"', ''))
    chinese = clean(paper.get('title_zh'))
    if chinese and re.search(r'[\u4e00-\u9fff]', chinese):
        result.extend((prefix + '"' + chinese + '"', prefix + chinese))
    authors = paper.get('authors') or []
    if isinstance(authors, str):
        authors = [authors]
    first_author = next((clean(a) for a in authors if isinstance(a, str) and clean(a)), '')
    for method in method_names(paper):
        result.append(prefix + '"' + method + '"')
        if first_author:
            result.append(prefix + '"' + method + '" "' + first_author + '"')
    return list(dict.fromkeys(result))


def local_candidate(paper, row):
    """Broad admission only; callers still need DOI or title/author verification."""
    text = clean(' '.join(str(row.get(k) or '') for k in ('title', 'summary', 'abstract', 'evidence_text')))
    normalized = _compact(text)
    doi = normalize_doi(paper.get('doi'))
    if doi and doi.casefold() in text.casefold():
        return True
    title = _compact(paper.get('title'))
    if len(title) >= 25 and title in normalized:
        return True
    if any(_compact(name) in normalized for name in method_names(paper)):
        return True
    chinese = ''.join(re.findall(r'[\u4e00-\u9fff]', clean(paper.get('title_zh'))))
    if len(chinese) >= 6:
        grams = {chinese[i:i + 2] for i in range(len(chinese) - 1)}
        hits = sum(g in normalized for g in grams)
        # Paraphrased Chinese headings remain candidates, not confirmed matches.
        if hits >= 4 and hits / len(grams) >= .2:
            return True
    words = {w.casefold() for w in re.findall(r'[A-Za-z]{5,}', str(paper.get('title') or ''))}
    words -= {'using', 'based', 'study', 'towards', 'through', 'approach', 'model', 'models', 'learning'}
    present = set(re.findall(r'[a-z]{5,}', text.casefold()))
    return bool(len(words) >= 4 and len(words & present) >= 4 and len(words & present) / len(words) >= .5)


def _safe_url(value):
    try:
        parsed = urlsplit(str(value or ''))
        return (parsed.scheme in ('http', 'https') and parsed.hostname in HOSTS
                and not parsed.username and not parsed.password and parsed.port in (None, 80, 443))
    except ValueError:
        return False


def request_page(url):
    """Bound response size and redirect hosts, without authenticating or scripting."""
    if not _safe_url(url):
        raise ValueError('Unapproved public index URL')

    class Redirects(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if not _safe_url(newurl):
                raise ValueError('Unapproved public index redirect')
            return super().redirect_request(request, fp, code, msg, headers, newurl)

    request = Request(url, headers={'User-Agent': 'DailyPapers/1.0 (+https://tengda-xmu.github.io/daily-papers/)'})
    with build_opener(Redirects).open(request, timeout=15) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Public index response too large')
        return {'body': body, 'url': response.geturl(), 'status': response.status, 'headers': dict(response.headers)}


def _page(transport, url):
    response = transport(url)
    if isinstance(response, bytes):
        response = {'body': response, 'url': url, 'status': 200}
    final_url = response.get('url') or url
    if not _safe_url(final_url):
        raise ValueError('Unapproved public index redirect')
    body = response.get('body', b'')
    if not isinstance(body, (str, bytes)) or len(body) > MAX_BYTES:
        raise ValueError('Invalid public index response')
    body = body.decode('utf-8', 'replace') if isinstance(body, bytes) else body
    challenge = body.lower() + ' ' + final_url.lower()
    if response.get('status') in (403, 429) or any(marker in challenge for marker in
            ('antispider', 'wappoc_appmsgcaptcha', '请输入验证码', '此验证码用于', '访问过于频繁', '环境异常', '访问验证')):
        error = PublicSearchChallenge('Public source requires verification')
        error.headers = response.get('headers', {})
        raise error
    if response.get('status', 200) >= 400:
        raise ValueError('Public source HTTP error')
    return body, final_url


def _resolve(href, base, transport):
    url = urljoin(base, html.unescape(href))
    visited = set()
    while _safe_url(url) and url not in visited:
        direct = article_url(url)
        if direct:
            return direct
        visited.add(url)
        body, final_url = _page(transport, url)
        direct = article_url(final_url)
        if direct:
            return direct
        soup = BeautifulSoup(body, 'html.parser')
        refresh = soup.find('meta', attrs={'http-equiv': re.compile('^refresh$', re.I)})
        match = re.search(r'url\s*=\s*[\"\']?([^\"\']+)', refresh.get('content', ''), re.I) if refresh else None
        target = match[1].strip() if match else ''
        if not target:
            # Literal redirects only: never execute obfuscated challenge scripts.
            match = re.search(r'(?:location(?:\.href)?\s*=\s*|location\.(?:replace|assign)\(\s*)[\"\']([^\"\']+)[\"\']', body)
            target = match[1].replace('\\/', '/') if match else ''
        if not target:
            return ''
        url = urljoin(final_url, html.unescape(target))
    return ''


def sogou_search(query, key='', *, transport=None):
    """Return SerpApi-like candidates plus truthful provider health.

    Unresolved result links become stable index links with reason=index_only.
    This function has no persistent cache or budget; its caller owns freshness.
    """
    transport = transport or request_page
    query = clean(re.sub(r'\bsite:mp\.weixin\.qq\.com\s*', '', query, flags=re.I))
    url = 'https://weixin.sogou.com/weixin?type=2&query=' + quote(query, safe='')
    rows, seen_rows, seen_pages, fingerprints = [], set(), set(), set()
    state, reason = 'ok', ''
    retry = {}

    def retry_hint(error):
        value = next((str(v) for k, v in (getattr(error, 'headers', {}) or {}).items()
                      if k.lower() == 'retry-after'), '')
        if value.isdigit():
            retry['retry_after'] = int(value)
        elif value:
            try:
                retry['retry_at'] = parsedate_to_datetime(value).isoformat()
            except (TypeError, ValueError, OverflowError):
                pass

    while url and url not in seen_pages:
        seen_pages.add(url)
        try:
            body, final_url = _page(transport, url)
            parsed = parse_search_page(body)
            # Expiring redirect tokens can change while the index repeats a page.
            identities = [(r['title'], r.get('account'), r.get('published_at')) for r in parsed['rows']]
            fingerprint = hashlib.sha256(repr(identities).encode()).hexdigest()
            if fingerprint in fingerprints:
                break
            fingerprints.add(fingerprint)
            for row in parsed['rows']:
                identity = (row['title'], row.get('account'), row.get('published_at'))
                if identity in seen_rows:
                    continue
                seen_rows.add(identity)
                candidate = {'title': row['title'], 'account': row.get('account', ''),
                             'published_at': row.get('published_at', ''), 'snippet': row.get('summary', '')}
                resolved = article_url(urljoin(final_url, row.get('href', '')))
                if not resolved and row.get('href') and state != 'blocked':
                    try:
                        resolved = _resolve(row['href'], final_url, transport)
                    except (PublicSearchChallenge, HTTPError) as exc:
                        if isinstance(exc, PublicSearchChallenge) or getattr(exc, 'code', None) in (403, 429):
                            state, reason = 'blocked', 'verification_required'
                            retry_hint(exc)
                        else:
                            state, reason = 'error', 'http_error'
                    except Exception:
                        state, reason = 'error', 'redirect_unavailable'
                candidate['link'] = resolved or public_index_url(candidate['account'], candidate['title'])
                if not resolved:
                    candidate['reason'] = 'index_only'
                rows.append(candidate)
            next_url = urljoin(final_url, parsed.get('next_url', '')) if parsed.get('next_url') else ''
            url = next_url if (state == 'ok' and _safe_url(next_url) and urlsplit(next_url).hostname == 'weixin.sogou.com'
                               and urlsplit(next_url).path == '/weixin') else ''
        except (PublicSearchChallenge, HTTPError) as exc:
            blocked = isinstance(exc, PublicSearchChallenge) or getattr(exc, 'code', None) in (403, 429)
            state, reason = ('blocked', 'verification_required') if blocked else ('error', 'http_error')
            retry_hint(exc)
            break
        except Exception:
            state, reason = 'error', 'unrecognized_or_unavailable'
            break
    return {'organic_results': rows, 'state': state, 'provider': 'sogou', 'reason': reason, **retry}
