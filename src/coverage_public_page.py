"""Discover explicitly attributed WeChat copies on ordinary public article pages."""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

from src.public_sources import clean, date
from src.wechat_metadata import article_url


def public_page_url(value):
    """Normalize public HTTPS URLs without permitting local or credential targets."""
    try:
        raw = str(value or '')
        parts = urlsplit(raw)
        host = (parts.hostname or '').lower()
        if (parts.scheme != 'https' or parts.username or parts.password
                or parts.port not in (None, 443) or any(ord(c) < 33 for c in raw)
                or '\\' in raw or '.' not in host or host.endswith('.')
                or host.endswith(('.localhost', '.local', '.internal', '.invalid', '.test', '.example'))
                or host in ('localhost', 'mp.weixin.qq.com', 'weixin.sogou.com', 'www.sogou.com')):
            return ''
        try:
            ipaddress.ip_address(host)
            return ''  # Article sources use public DNS names, never literal addresses.
        except ValueError:
            pass
        if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host):
            return ''
        path = unquote(parts.path)
        if any(part in ('.', '..') for part in path.split('/')):
            return ''
        query = []
        for key, val in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower().replace('-', '_')
            if (re.search(r'(?:^|_)(?:token|secret|password|credential|signature|authorization|auth|cookie|sessionid)(?:$|_)', lowered)
                    or lowered in ('api_key', 'apikey', 'key', 'session', 'jwt', 'sid')):
                return ''
            if not key.lower().startswith(('utm_', 'fbclid', 'gclid', 'spm')):
                query.append((key, val))
        return urlunsplit(('https', host, parts.path or '/', urlencode(query), ''))
    except ValueError:
        return ''


def require_public_host(url):
    """Check every destination, including each redirect, before fetching it."""
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Non-public article destination')


def attributed_article(soup):
    """Return a main article and its own explicit source; never navigation links."""
    main = next((node for selector in ('#js_content', '#mp-editor', '[itemprop="articleBody"]',
                 '.article-content', '.article_content', '.entry-content',
                 'td.t_f[id^="postmessage_"]', 'article', 'main')
                 if (node := soup.select_one(selector)) is not None), None)
    if main is None:
        return None
    # Related reports are not provenance for the containing article.
    for node in main.select('aside, nav, .related, .recommend, .references, .reference-list, '
                            '.recommendations, .related-posts, script, style, form'):
        node.decompose()
    if len(main.select('article, td.t_f[id^="postmessage_"]')) > 1:
        return None
    # Identify explicit end sections even when the heading is a bold paragraph.
    # After that boundary, neither a source link nor a related DOI is evidence.
    after_end, ended = set(), False
    for node in main.descendants:
        if getattr(node, 'name', None) in ('p', 'h2', 'h3', 'h4', 'strong', 'b'):
            label = clean(node.get_text(' ', strip=True))
            if len(label) < 80 and re.match(r'^(?:参考文献|参考资料|参考链接|推荐阅读|相关阅读|references|bibliography|'
                                          r'(?:related|recommended|further)\s+(?:reading|articles))', label, re.I):
                ended = True
        if ended:
            after_end.add(id(node))
    byline = main.find_previous_sibling()
    if byline and (byline.name not in ('p', 'div', 'section')
                   or byline.select('article, table, main') or len(clean(byline.get_text())) > 300):
        byline = None

    def in_source_scope(node):
        inside = main in node.parents
        return (inside and id(node) not in after_end) or (
            not inside and byline is not None and (node is byline or byline in node.parents))

    sources = {}
    for link in soup.select('a[href]'):
        original = article_url(link.get('href'))
        if not original or not in_source_scope(link) or link.find_parent(['nav', 'aside']):
            continue
        parent = link.parent
        label = clean(parent.get_text(' ', strip=True)) if parent else ''
        # Source attribution must be an isolated byline, not a whole document
        # that happens to contain both a source heading and a recommended link.
        if len(label) > 300 or not re.search(r'来源|转载[自于]|原文(?:链接|地址)|公众号|original(?:ly)?|source', label, re.I):
            continue
        if re.search(r'参考文献|参考资料|相关阅读|推荐阅读|参考链接', label):
            continue
        classes = ' '.join(str(p.get('class', '')) for p in [link, *link.parents] if p)
        if re.search(r'related|recommend|reference', classes, re.I):
            continue
        heading = link.find_previous(['h2', 'h3', 'h4'])
        if heading and re.match(r'^(?:参考|推荐阅读|相关阅读|references|bibliography)', clean(heading.get_text()), re.I):
            continue
        text = clean(link.get_text(' ', strip=True))
        account = re.sub(r'^(?:来源|转载自|微信公众号|公众号)\s*[:：]?\s*', '', text).strip()
        if (not account or account.startswith(('http', '原文', '阅读', '查看', '点击'))
                or account.lower() in ('source', 'original', 'wechat')):
            match = re.search(r'(?:来源|转载自|微信公众号|公众号)\s*[:：]?\s*([^，。；|\n]{2,80})', label)
            account = match[1].strip() if match else ''
            account = re.split(r'\s*(?:原文链接|阅读原文|查看原文|https?://)', account)[0].strip()
        if not account or len(account) > 100:
            continue
        sources[original] = account
    # Forums sometimes preserve the source URL as plain text. Read only an
    # explicit attribution line inside the first article, not replies or nav.
    for node in main.select('p'):
        if not in_source_scope(node):
            continue
        label = clean(node.get_text(' ', strip=True))
        match = re.fullmatch(r'(?:本文)?(?:转载[于自]|来源)\s*[:：]?\s*([^:：]{2,80})\s*[:：]\s*'
                             r'(https://mp\.weixin\.qq\.com/\S+)', label)
        if not match or len(label) > 500:
            continue
        heading = node.find_previous(['h2', 'h3', 'h4'])
        if heading and re.match(r'^(?:参考|推荐阅读|相关阅读|references|bibliography)', clean(heading.get_text()), re.I):
            continue
        original = article_url(match[2])
        if original:
            sources[original] = match[1].strip()
    if len(sources) != 1:
        return None  # Ambiguous multi-report pages require a dedicated parser.
    original, account = next(iter(sources.items()))
    title_node = soup.select_one('h1')
    title_meta = soup.select_one('meta[property="og:title"]')
    title = clean(title_node.get_text()) if title_node else clean(title_meta.get('content')) if title_meta else ''
    if not title:
        return None
    # Do not mistake the syndicator's publication date for the original date.
    published = ''
    original_time = soup.select_one('[data-original-published], [itemprop="originalPublicationDate"]')
    if original_time:
        published = date(original_time.get('data-original-published') or original_time.get('content') or original_time.get_text())
    # Attribution boundaries also bound paper identity and visual evidence.
    # Passing a related paper's DOI or cover image to the identity matcher
    # would incorrectly attach it to this article's otherwise valid account.
    for node in list(main.descendants):
        if id(node) in after_end and main in node.parents:
            node.extract()
    return {'content': str(main), 'original': original, 'account': account,
            'title': title, 'published': published}
