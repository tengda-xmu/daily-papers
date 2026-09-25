"""Bounded public announcement collection. Source text is data, never instructions."""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from src.models import parse_date

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 4_000_000


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def clean(value):
    text = str(value or '')
    if '<' in text:
        text = BeautifulSoup(text, 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text).strip()


def canonical(url):
    parts = urlsplit(str(url or ''))
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
        return ''
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(('utm_', 'fbclid', 'gclid'))])
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or '/', query, parts.fragment))


def identifier(url):
    return 'public-' + hashlib.sha256(canonical(url).encode()).hexdigest()[:24]


def date(value):
    raw = str(value or '').strip()
    stamp = parse_date(raw)
    if not stamp and raw:
        try:
            stamp = parsedate_to_datetime(raw)
        except (ValueError, TypeError, OverflowError):
            match = re.search(r'(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})', raw)
            if match:
                try:
                    stamp = datetime(*map(int, match.groups()), tzinfo=timezone.utc)
                except ValueError:
                    pass
            if not stamp:
                match = re.search(r'(?:[A-Za-z]{3,9} \d{1,2},? 20\d{2}|\d{1,2} [A-Za-z]{3,9} 20\d{2})', raw)
                if match:
                    for fmt in ('%B %d, %Y', '%b %d, %Y', '%B %d %Y', '%b %d %Y', '%d %B %Y', '%d %b %Y'):
                        try:
                            stamp = datetime.strptime(match[0], fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            pass
    return stamp.isoformat() if stamp else ''


def allowed(url, source):
    return bool(canonical(url)) and urlsplit(url).hostname in source['hosts']


def fetch(url, source):
    if not allowed(url, source):
        raise ValueError('Unapproved source host')
    class Redirects(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, newurl):
            if not allowed(newurl, source):
                raise ValueError('Unapproved redirect')
            return super().redirect_request(request, fp, code, msg, headers, newurl)
    request = Request(url, headers={'User-Agent': 'DailyPapers/1.0 (+https://tengda-xmu.github.io/daily-papers/)'})
    with build_opener(Redirects).open(request, timeout=15) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Announcement too large')
        return body


def article(body, url):
    if body.startswith(b'%PDF'):
        import fitz
        with fitz.open(stream=body, filetype='pdf') as doc:
            text = '\n'.join(page.get_text() for page in list(doc)[:15])
        return {'title': clean(text.splitlines()[0] if text.splitlines() else ''), 'evidence_text': clean(text)[:18000], 'url': url}
    try:
        body = body.decode('utf-8')
    except UnicodeDecodeError:
        pass
    soup = BeautifulSoup(body, 'html.parser')
    heading = soup.select_one('h1')
    title = clean(heading.get_text() if heading else (soup.title.get_text() if soup.title else ''))
    published = ''
    for selector in ('meta[property="article:published_time"]', 'meta[name="citation_publication_date"]', 'meta[name="pubdate"]', 'meta[name="date"]', 'meta[name="PubDate"]', 'meta[name="publishdate"]'):
        node = soup.select_one(selector)
        if node:
            published = date(node.get('content'))
            if published:
                break
    if not published:
        node = soup.select_one('time[datetime]')
        published = date(node.get('datetime')) if node else ''
    if not published:
        for node in soup.find_all(class_=re.compile(r'date|publish', re.I)):
            label = clean(node.get_text())
            if len(label) < 80 and not re.search(r'closing|deadline|modified|updated|截止|更新', label, re.I):
                published = date(label)
                if published:
                    break
    if not published:
        for node in soup.find_all('script', type='application/ld+json'):
            try:
                def dates(value):
                    if isinstance(value, dict):
                        if value.get('datePublished'):
                            yield value['datePublished']
                        for child in value.values():
                            yield from dates(child)
                    elif isinstance(value, list):
                        for child in value:
                            yield from dates(child)
                published = next((date(d) for d in dates(json.loads(node.string or '{}')) if date(d)), '')
            except ValueError:
                pass
            if published:
                break
    for node in soup.select('script,style,nav,header,footer,aside,form'):
        node.decompose()
    main = soup.select_one('.TRS_Editor, .v_news_content, .entry-content, .markdown, .prose') or soup.select_one('article') or soup.select_one('main') or soup.body or soup
    text = clean(main.get_text(' ', strip=True))
    # Main content selectors may omit the publication header, so consult the page too.
    page_text = clean(soup.get_text(' ', strip=True))
    labeled = re.search(r'Publication date\s*:\s*(\d{1,2} [A-Za-z]+ 20\d{2})', page_text, re.I)
    if labeled:
        published = date(labeled[1])
    # Explicit publication labels only; never mistake an application deadline for publication.
    if not published:
        match = re.search(r'(?:^|[\s，。；;])(?:发布时间|发布日期|时间|日期|Date)\s*[:：]\s*(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})', page_text)
        published = date(match[1]) if match else ''
        if not published:
            match = re.search(r'Published(?: on)?\s*((?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2},? 20\d{2})', text[:1200])
            published = date(match[1]) if match else ''
    attachments = [{'label': clean(a.get_text()) or '官方附件', 'url': canonical(urljoin(url, a['href']))}
                   for a in main.select('a[href]') if re.search(r'\.(pdf|docx?|xlsx?)(?:\?|$)', a['href'], re.I)]
    application_links = [canonical(urljoin(url,a['href'])) for a in main.select('a[href]')
                         if re.search(r'full opportunity details|apply now|申请入口|申报入口',clean(a.get_text()),re.I)]
    intro = next((clean(p.get_text(' ',strip=True)) for p in main.select('p') if len(clean(p.get_text())) >= 80), text)
    result = {'title': title, 'published_at': published, 'evidence_text': text[:18000], 'metadata_text': page_text[:24000],
              'summary': intro[:220], 'url': canonical(url), 'attachments': attachments[:12]}
    if application_links:
        result['application_url'] = application_links[0]
    return result


def candidates(body, source):
    mode = source.get('format', 'html')
    if mode == 'document':
        return [article(body, source['url'])]
    if mode in ('rss', 'atom', 'releases'):
        if b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
            raise ValueError('Unsupported XML declaration')
        root = ET.fromstring(body)
        if root.tag.rsplit('}', 1)[-1] not in ('rss', 'feed'):
            raise ValueError('Expected RSS or Atom')
        result = []
        for node in root.findall('./channel/item') + root.findall('{*}entry'):
            link = node.findtext('link') or next((n.get('href') for n in node.findall('{*}link') if n.get('rel', 'alternate') == 'alternate'), '')
            title = clean(node.findtext('title') or node.findtext('{*}title'))
            published = date(node.findtext('pubDate') or node.findtext('{*}published') or node.findtext('{*}updated'))
            summary = clean(node.findtext('description') or node.findtext('{*}summary') or node.findtext('{*}content'))
            result.append({'title': title, 'url': canonical(link), 'published_at': published, 'summary': summary[:220], 'evidence_text': summary[:18000]})
        return result
    soup = BeautifulSoup(body, 'html.parser')
    if mode == 'changelog':
        result = []
        for heading in soup.select(source.get('selector', 'h2')):
            published = date(heading.get_text())
            if not published:
                continue
            pieces = []
            for sibling in heading.next_elements:
                if getattr(sibling, 'name', '') == heading.name:
                    break
                if getattr(sibling, 'name', '') in ('h3', 'p', 'li'):
                    pieces.append(sibling.get_text(' ', strip=True))
            text = clean(' '.join(pieces))
            title = clean(pieces[0]) if pieces else ''
            if title:
                result.append({'title': title, 'published_at': published, 'url': source['url'].split('#')[0] + '#' + heading.get('id', published[:10]), 'summary': text[:220], 'evidence_text': text[:18000]})
        return result
    result, seen = [], set()
    for node in soup.select(source.get('selector', 'a[href]')):
        if source.get('card_selector'):
            card = node.find_parent(class_=source['card_selector'])
            label = card.select_one('h2,h3') if card else None
        else:
            label = None
        link = canonical(urljoin(source['url'], node.get('href', '')))
        title = clean(label.get_text() if label else node.get('title') or node.get_text())
        if not title and source.get('card_titles'):
            for parent in list(node.parents)[:4]:
                heading = parent.select_one('h2,h3')
                if heading:
                    title = clean(heading.get_text())
                    break
        if len(title) < 10 or not allowed(link, source) or link in seen:
            continue
        if source.get('link_pattern') and not re.search(source['link_pattern'], link):
            continue
        if source.get('include') and not re.search(source['include'], title, re.I):
            continue
        seen.add(link)
        result.append({'title': title, 'url': link})
    return result


def collect_source(source, now, fetcher=fetch):
    body = fetcher(source['url'], source)
    rows = candidates(body, source)
    rows = [r for r in rows if (not source.get('include') or re.search(source['include'], r.get('title', ''), re.I))
            and (not source.get('exclude') or not re.search(source['exclude'], r.get('title', ''), re.I))]
    result = []
    for row in rows[:source.get('limit', 8)]:
        if not allowed(row.get('url', ''), source):
            continue
        if source.get('include') and not re.search(source['include'], row.get('title', ''), re.I):
            continue
        try:
            if source.get('details', source.get('format', 'html') == 'html'):
                detail = article(fetcher(row['url'], source), row['url'])
                row = {**row, **{k: v for k, v in detail.items() if v}}
        except Exception:
            row['verification'] = 'pending'
        published = parse_date(row.get('published_at'))
        if published and published > now:
            continue
        if not row.get('evidence_text'):
            row['verification'] = 'pending'
        if row.get('title', '').casefold() in ('news', 'funding finder', 'blog', 'access denied', 'just a moment...'):
            continue
        if source.get('format') == 'releases':
            row['product_version'] = row['title']
            row['title'] = source.get('defaults', {}).get('product', source['name']) + ' ' + row['title']
        row['source_version'] = hashlib.sha256(clean(row.get('evidence_text')).encode()).hexdigest()
        review = source.get('review', {})
        if review.get('source_version') == row['source_version']:
            row.update(review.get('fields', {}))
        result.append({**source.get('defaults', {}), **row, 'id': identifier(row['url']),
            'source_id': source['id'], 'source': source['name'], 'organization': source.get('organization', source['name']),
            'provider': 'official', 'checked_at': now.isoformat(),
            'verified_at': now.isoformat() if row.get('verification') != 'pending' else '',
            'verification': row.get('verification', 'verified'), 'evidence_url': row['url']})
    return result


def refresh_sources(config, previous, now, fetcher=fetch):
    old = {s['id']: s for s in previous.get('sources', [])}
    sources = config.get('sources', [])
    def work(source):
        status = {'id': source['id'], 'name': source['name'], 'url': source['url'], 'checked_at': now.isoformat(),
                  'last_success': old.get(source['id'], {}).get('last_success', '')}
        try:
            rows = collect_source(source, now, fetcher)
            status.update(status='ok' if rows else 'no_data', count=len(rows), last_success=now.isoformat())
            return rows, status
        except Exception as exc:
            status.update(status='error', count=0, error=type(exc).__name__)
            return [], status
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(work, sources))
    return [r for rows, _ in results for r in rows], [s for _, s in results]
