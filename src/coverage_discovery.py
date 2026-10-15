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

SEARCH_STRATEGY_VERSION = 5
HOSTS = {'weixin.sogou.com', 'www.sogou.com', 'mp.weixin.qq.com'}
MAX_BYTES = 4_000_000
GENERIC_METHODS = {'DOI', 'PDF', 'HTML', 'HTTP', 'HTTPS', 'AI', 'ML', 'LLM', 'CNS', 'RMSE', 'MAPE',
                   'PMID', 'ISSN', 'ISBN', 'FIG', 'FIGURE', 'TABLE', 'ABSTRACT', 'REFERENCES',
                   'BIBLIOGRAPHY', 'INTRODUCTION', 'METHODS', 'RESULTS', 'DISCUSSION', 'CONCLUSION',
                   'CONCLUSIONS', 'ACKNOWLEDGMENTS', 'ACKNOWLEDGEMENTS', 'SUPPLEMENTARY'}
GENERIC_CHINESE = (
    '人工智能', '机器学习', '深度学习', '强化学习', '批判性', '系统性', '最新进展',
    '研究进展', '研究现状', '研究方法', '研究成果', '中的', '通过', '基于', '面向',
    '方法', '应用', '研究', '综述', '挑战', '技术', '模型', '引导', '发现', '新颖',
    '多样', '关于', '介绍', '未来', '展望', '及其', '以及', '智能', '人工', '学习',
)


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
    hint_text = _text(paper)
    for match in re.finditer(r'(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*', hint_text):
        token = match.group()
        if token.upper() in GENERIC_METHODS or len(token) < 4 or re.fullmatch(r'P\d+', token):
            continue
        before = hint_text[max(0, match.start() - 100):match.start()]
        if re.search(r'\b(?:grant|contract|award|funding)(?:\s+(?:number|no\.?|nos\.?))?[\s#:\-]*$', before, re.I):
            continue  # Funding identifiers are not inferred method names.
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
    # Introductions often paraphrase a review's subtitle. Retain its concrete
    # subject as another discovery query, while identity confirmation remains
    # DOI or full title/author based. Do not reduce headings to a generic AI tag.
    chinese_subject = re.split(r'[:：]', chinese, maxsplit=1)[0].strip()
    if chinese_subject != chinese and len(re.findall(r'[\u4e00-\u9fff]', chinese_subject)) >= 6:
        result.append(prefix + chinese_subject)
    english_subject = re.split(r'[:：]', normalized, maxsplit=1)[0].strip()
    subject_words = set(re.findall(r'[A-Za-z]{3,}', english_subject.casefold()))
    subject_words -= {'the', 'and', 'for', 'with', 'from', 'into', 'via'}
    if english_subject != normalized and first_author and len(subject_words) >= 3:
        result.append(prefix + '"' + english_subject.replace('"', '') + '" "' + first_author + '"')
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
    chinese = clean(paper.get('title_zh'))
    if len(re.findall(r'[\u4e00-\u9fff]', chinese)) >= 6:
        # The subject before a subtitle is the useful topical anchor. Remove
        # whole generic phrases before making grams, so "人工智能" cannot leave
        # misleading internal "工智" fragments or join unrelated words.
        subject = re.split(r'[:：]', chinese, maxsplit=1)[0]
        subject = re.sub('|'.join(sorted(GENERIC_CHINESE, key=len, reverse=True)), ' ', subject)
        parts = [part.strip('的与和及且在') for part in re.findall(r'[\u4e00-\u9fff]+', subject)]
        grams = {part[i:i + 2] for part in parts for i in range(len(part) - 1)}
        hits = sum(g in normalized for g in grams)
        # Broad topical admission is still distinct from identity confirmation.
        if grams and hits >= min(2, len(grams)) and hits / len(grams) >= .2:
            return True
    words = {w.casefold() for w in re.findall(r'[A-Za-z]{5,}', str(paper.get('title') or ''))}
    words -= {'using', 'based', 'study', 'towards', 'through', 'approach', 'model', 'models', 'learning'}
    present = set(re.findall(r'[a-z]{5,}', text.casefold()))
    return bool(len(words) >= 4 and len(words & present) >= 4 and len(words & present) / len(words) >= .5)


def _safe_url(value):
    try:
        value = str(value or '')
        if any(ord(char) < 32 for char in value):
            return False
        parsed = urlsplit(value)
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

    # Public index hrefs sometimes contain an unescaped query-space or Chinese
    # text. Encode those characters while preserving existing percent escapes.
    url = quote(url, safe=":/?#[]@!$&'()*+,;=%")
    request = Request(url, headers={'User-Agent': 'DailyPapers/1.0 (+https://tengda-xmu.github.io/daily-papers/)'})
    with build_opener(Redirects).open(request, timeout=15) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Public index response too large')
        return {'body': body, 'url': response.geturl(), 'status': response.status, 'headers': dict(response.headers)}


def _page(transport, url):
    if not _safe_url(url):
        raise ValueError('Unapproved public index URL')
    url = quote(url, safe=":/?#[]@!$&'()*+,;=%")
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


def sogou_search(query, key='', *, transport=None, resolve_links=True):
    """Return candidates with independent index and link-resolution health.

    Unresolved result links become stable index links with reason=index_only.
    This function has no persistent cache or budget; its caller owns freshness.
    Top-level state/retry hints describe index requests only. A challenge while
    following a result link stops that channel, not the available index. Callers
    may set resolve_links=False during its separate retry interval.
    """
    transport = transport or request_page
    query = clean(re.sub(r'\bsite:mp\.weixin\.qq\.com\s*', '', query, flags=re.I))
    url = 'https://weixin.sogou.com/weixin?type=2&query=' + quote(query, safe='')
    rows, seen_rows, seen_pages, fingerprints = [], set(), set(), set()
    state, reason = 'ok', ''
    retry = {}
    resolution = {'state': 'ok' if resolve_links else 'deferred', 'reason': '', 'attempted': False}

    def retry_hint(error, target):
        value = next((str(v) for k, v in (getattr(error, 'headers', {}) or {}).items()
                      if k.lower() == 'retry-after'), '')
        if value.isdigit():
            target['retry_after'] = int(value)
        elif value:
            try:
                target['retry_at'] = parsedate_to_datetime(value).isoformat()
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
                if not resolved and row.get('href') and resolution['state'] == 'ok':
                    try:
                        resolution['attempted'] = True
                        resolved = _resolve(row['href'], final_url, transport)
                    except (PublicSearchChallenge, HTTPError) as exc:
                        if isinstance(exc, PublicSearchChallenge) or getattr(exc, 'code', None) in (403, 429):
                            resolution.update(state='blocked', reason='verification_required')
                            retry_hint(exc, resolution)
                        else:
                            resolution.update(state='error', reason='http_error')
                    except Exception:
                        resolution.update(state='error', reason='redirect_unavailable')
                candidate['link'] = resolved or public_index_url(candidate['account'], candidate['title'])
                if not resolved:
                    candidate['reason'] = 'index_only'
                    redirect = urljoin(final_url, html.unescape(row.get('href', '')))
                    if _safe_url(redirect) and urlsplit(redirect).path == '/link':
                        # Retain the normal result link privately so resolution
                        # can resume independently after its access restriction.
                        candidate['_redirect_url'] = redirect
                rows.append(candidate)
            next_url = urljoin(final_url, parsed.get('next_url', '')) if parsed.get('next_url') else ''
            url = next_url if (state == 'ok' and _safe_url(next_url) and urlsplit(next_url).hostname == 'weixin.sogou.com'
                               and urlsplit(next_url).path == '/weixin') else ''
        except (PublicSearchChallenge, HTTPError) as exc:
            blocked = isinstance(exc, PublicSearchChallenge) or getattr(exc, 'code', None) in (403, 429)
            state, reason = ('blocked', 'verification_required') if blocked else ('error', 'http_error')
            retry_hint(exc, retry)
            break
        except Exception:
            state, reason = 'error', 'unrecognized_or_unavailable'
            break
    return {'organic_results': rows, 'state': state, 'provider': 'sogou', 'reason': reason,
            'link_resolution': resolution, **retry}
