"""Read attributed public copies without executing publisher page JavaScript.

These records are private evidence about a WeChat source. A readable copy never
implies that the WeChat page is readable or that republication is authorized.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from html import escape
import json
import re
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from bs4 import BeautifulSoup, NavigableString

from src.models import parse_date
from src.public_sources import MAX_BYTES, clean
from src.wechat_metadata import article_url

PATHS = {
    'cloud.tencent.com': re.compile(r'/developer/article/\d+/?\Z'),
    'hub.baai.ac.cn': re.compile(r'/view/\d+/?\Z'),
    'synapse.zhihuiya.com': re.compile(r'/(?:organization/[a-f0-9]{24,64}|news-detail/[A-Za-z0-9_-]+)/?\Z'),
}
IMAGE_HOSTS = ['mmbiz.qpic.cn', 'mmbiz.qlogo.cn', 'developer.qcloudimg.com', 'simg.baai.ac.cn']


def canonical_syndication_url(value):
    """Return an exact allowlisted public page URL, without tracking parameters."""
    try:
        parts = urlsplit(str(value or ''))
        if (parts.scheme != 'https' or parts.hostname not in PATHS
                or parts.username or parts.password or parts.port not in (None, 443)
                or not PATHS[parts.hostname].fullmatch(parts.path)
                or any(ord(c) < 33 for c in str(value))):
            return ''
        return urlunsplit(('https', parts.hostname, parts.path.rstrip('/'), '', ''))
    except ValueError:
        return ''


def is_syndication_url(value):
    """Only normalized, parameter-free mirror links may cross the public boundary."""
    return bool(value and canonical_syndication_url(value) == str(value))


def fetch_syndication(url):
    url = canonical_syndication_url(url)
    if not url:
        raise ValueError('Unapproved syndicated page')
    host = urlsplit(url).hostname

    class Redirects(HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, headers, target):
            if not is_syndication_url(target) or urlsplit(target).hostname != host:
                raise ValueError('Unapproved syndicated redirect')
            return super().redirect_request(request, fp, code, msg, headers, target)

    request = Request(url, headers={'User-Agent': 'DailyPapers/1.0'})
    with build_opener(Redirects).open(request, timeout=15) as response:
        if not is_syndication_url(response.url) or urlsplit(response.url).hostname != host:
            raise ValueError('Unapproved syndicated response')
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('Syndicated page too large')
        return body


class _Literal:
    """Small data-only reader for Nuxt's object literals, never an evaluator."""
    def __init__(self, text, bindings=None):
        self.text, self.pos, self.bindings = text, 0, bindings or {}

    def space(self):
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1

    def value(self, depth=0):
        if depth > 40:
            raise ValueError('Nested page data')
        self.space()
        if self.pos >= len(self.text):
            raise ValueError('Incomplete page data')
        char = self.text[self.pos]
        if char == '"':
            value, size = json.JSONDecoder().raw_decode(self.text[self.pos:])
            self.pos += size
            return value
        if char in '{[':
            self.pos += 1
            result = {} if char == '{' else []
            closer = '}' if char == '{' else ']'
            while True:
                self.space()
                if self.pos >= len(self.text):
                    raise ValueError('Incomplete page container')
                if self.text[self.pos] == closer:
                    self.pos += 1
                    return result
                if char == '{':
                    if self.text[self.pos] == '"':
                        key = self.value(depth + 1)
                    else:
                        token = re.match(r'[A-Za-z_$][\w$]*', self.text[self.pos:])
                        if not token:
                            raise ValueError('Invalid object key')
                        key = token[0]
                        self.pos += len(key)
                    self.space()
                    if self.pos >= len(self.text) or self.text[self.pos] != ':':
                        raise ValueError('Invalid object separator')
                    self.pos += 1
                    result[key] = self.value(depth + 1)
                else:
                    result.append(self.value(depth + 1))
                self.space()
                if self.pos < len(self.text) and self.text[self.pos] == ',':
                    self.pos += 1
                elif self.pos >= len(self.text) or self.text[self.pos] != closer:
                    raise ValueError('Invalid data separator')
        token = re.match(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[A-Za-z_$][\w$]*', self.text[self.pos:])
        if not token:
            raise ValueError('Not literal page data')
        word = token[0]
        self.pos += len(word)
        if word in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[word]
        if re.match(r'-?\d', word):
            return json.loads(word)
        if word not in self.bindings:
            raise ValueError('Unknown page data binding')
        return self.bindings[word]


def _nuxt_post(soup):
    for node in soup.select('script'):
        text = node.get_text()
        if 'window.__NUXT__=' not in text or 'post_data:' not in text:
            continue
        bindings = {}
        signature = re.search(r'window\.__NUXT__\s*=\s*\(function\(([^)]*)\)\{return\s', text)
        if signature:
            tail = re.search(r'\}\((.*)\)\);?\s*$', text, re.S)
            if not tail:
                continue
            args = _Literal('[' + tail[1] + ']').value()
            names = [name.strip() for name in signature[1].split(',')]
            if len(args) != len(names):
                continue
            bindings = dict(zip(names, args))
        start = re.search(r'\bpost_data\s*:\s*\{', text)
        if start:
            return _Literal(text[start.end() - 1:], bindings).value()
    return {}


def _stamp(value):
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return ''
    parsed = parse_date(str(value or ''))
    return parsed.isoformat() if parsed else ''


def _record(content, original, mirror, body, title='', account='', author='', published=''):
    from src.paper_coverage import parse_article
    url = article_url(original)
    if not url:
        return None
    # Only the supplied article container participates in identity evidence.
    inner = BeautifulSoup(content, 'html.parser')
    main = inner.select_one('#js_content')
    if main is None:
        main = inner.new_tag('div', id='js_content')
        for node in list(inner.contents):
            main.append(node.extract())
    article = parse_article(str(main).encode('utf-8'), url, image_hosts=IMAGE_HOSTS)
    article.update({
        'title': clean(title), 'account': clean(account), 'author': clean(author),
        'published_at': _stamp(published), 'source_version': hashlib.sha256(body).hexdigest(),
        'mirror_url': mirror, 'mirror_kind': 'collection' if '/organization/' in mirror else 'article',
        'mirror_body_status': 'readable', 'evidence_source': 'syndicated_article',
    })
    return article


def _draft_html(content):
    if not isinstance(content, dict):
        return ''
    entities = content.get('entityMap', {})
    output = []
    for block in content.get('blocks', []):
        if not isinstance(block, dict):
            continue
        if block.get('type') == 'atomic':
            for reference in block.get('entityRanges', []):
                entity = entities.get(str(reference.get('key')), {})
                data = entity.get('data') or {}
                if entity.get('type') == 'IMAGE':
                    output.append('<img src="' + escape(str(data.get('imageUrl', '')), quote=True)
                                  + '" alt="' + escape(str(data.get('imageAlt', '')), quote=True) + '">')
        else:
            text = str(block.get('text', ''))
            # Preserve a linked DOI as identity evidence when an actual LINK
            # entity supplies the anchor; no snippets or unrelated links added.
            links = []
            for reference in block.get('entityRanges', []):
                entity = entities.get(str(reference.get('key')), {})
                if entity.get('type') == 'LINK':
                    href = str((entity.get('data') or {}).get('url', ''))
                    if href.startswith(('https://', 'http://')):
                        links.append('<a href="' + escape(href, quote=True) + '"></a>')
            tag = 'h2' if str(block.get('type', '')).startswith('header') else 'p'
            output.append('<' + tag + '>' + escape(text) + ''.join(links) + '</' + tag + '>')
    return ''.join(output)


def _tencent(soup, url, body):
    node = soup.select_one('script#__NEXT_DATA__')
    if node is None:
        return []
    data = json.loads(node.get_text())
    fallback = data.get('props', {}).get('pageProps', {}).get('fallback', {})
    result, recognized = [], False
    for item in fallback.values() if isinstance(fallback, dict) else []:
        if not isinstance(item, dict) or not isinstance(item.get('articleInfo'), dict):
            continue
        info = item['articleInfo']
        if info.get('articleId') and str(info['articleId']) != url.rstrip('/').rsplit('/', 1)[-1]:
            continue
        recognized = True
        source = item.get('sourceDetail') or info.get('sourceDetail') or {}
        content = info.get('html') or _draft_html(info.get('content'))
        if not isinstance(source, dict) or not content:
            continue
        row = _record(content, source.get('sourceLink'), url, body, info.get('title'),
                      source.get('wechatNickName'), source.get('sourceAuthor'), source.get('originalTime'))
        if row:
            result.append(row)
    if not recognized:
        raise ValueError('No syndicated Tencent article')
    return result


def _baai(soup, url, body):
    post = _nuxt_post(soup)
    if not isinstance(post, dict) or not article_url(post.get('url')):
        return []
    if post.get('id') and str(post['id']) != url.rstrip('/').rsplit('/', 1)[-1]:
        raise ValueError('Mismatched syndicated article id')
    main = soup.select_one('#post-content #js_content')
    content = str(main) if main else post.get('content', '')
    title = post.get('title') or (soup.h1.get_text() if soup.h1 else '')
    account = post.get('source_name') or post.get('source') or post.get('author') or ''
    # Nuxt publisher aliases vary; never treat the reposting user as the author.
    if not isinstance(account, str):
        account = ''
    original_date = post.get('original_time') or ''
    row = _record(content, post['url'], url, body, title, account, '', original_date)
    return [row] if row else []


def _synapse(soup, url, body):
    result = []
    for card in soup.select('[data-testid="news-document-card"]'):
        title = card.select_one('[data-testid="news-document-card__title"]')
        content = card.select_one('[data-testid="news-document-card__content"]')
        if title is None or content is None or not article_url(title.get('href')):
            continue
        source = next((link for link in card.select('a[href]')
                       if link is not title and article_url(link.get('href')) == article_url(title.get('href'))), None)
        stamp = re.search(r'\b20\d{2}-\d{2}-\d{2}\b', card.get_text(' ', strip=True))
        # Synapse puts a whole article in a single text node. Restore paragraph
        # boundaries so reference headings cannot contribute matching evidence.
        if not content.select('p, section, h2, h3'):
            fragments = []
            for child in content.children:
                if isinstance(child, NavigableString):
                    fragments.extend('<p>' + escape(line) + '</p>' for line in str(child).splitlines() if line.strip())
                else:
                    fragments.append(str(child))
            article = ''.join(fragments)
        else:
            article = str(content)
        row = _record(article, title.get('href'), url, body, title.get_text(),
                      source.get_text() if source else '', '', stamp[0] if stamp else '')
        if row:
            result.append(row)
    return result


def parse_syndication(body, url):
    """Extract explicitly attributed source articles from a supported copy page."""
    canonical = canonical_syndication_url(url)
    if not canonical or not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError('Invalid syndicated response')
    decoded = body.decode('utf-8', 'replace')
    soup = BeautifulSoup(decoded, 'html.parser')
    host = urlsplit(canonical).hostname
    expected = {
        'cloud.tencent.com': soup.select_one('script#__NEXT_DATA__') is not None,
        'hub.baai.ac.cn': any('post_data:' in node.get_text() for node in soup.select('script')),
        'synapse.zhihuiya.com': soup.select_one('[data-testid="news-document-card"]') is not None,
    }[host]
    if not expected:
        if re.search(r'captcha|wappoc|验证码|访问受限|验证身份|登录后|login required|sign in to continue', decoded, re.I):
            from src.paper_coverage import ArticleChallenge
            raise ArticleChallenge('Syndicated page requires verification')
        raise ValueError('No recognized syndicated article container')
    parser = {'cloud.tencent.com': _tencent, 'hub.baai.ac.cn': _baai,
              'synapse.zhihuiya.com': _synapse}[host]
    return parser(soup, canonical, body)
