"""Paper-linked public-account introductions and authorized static reprints."""
from html import escape
from copy import deepcopy
import json
import re


def article_name(row):
    # URL-derived identities are independent of paper IDs, permitting one
    # authorized article to introduce several papers without duplicate pages.
    import hashlib
    return hashlib.sha256(row['url'].encode()).hexdigest()[:24]


def reprint_available(row):
    """Only link a processed, authorized article with renderable body text."""
    return row.get('display_mode') == 'reprint' and any(
        isinstance(block, dict) and block.get('type') in ('heading', 'paragraph')
        and isinstance(block.get('text'), str) and block['text'].strip()
        for block in (row.get('blocks') or []))


def coverage_panel(paper_id, coverage=None, *, root='./', suffix=''):
    """An introduction opens its article immediately; extra sources are optional."""
    from src.models import parse_date
    from src.wechat_metadata import article_url
    coverage = coverage or {}
    verified = []
    for row in coverage.get('articles', []):
        if not isinstance(row, dict) or row.get('match_status') != 'verified':
            continue
        url = article_url(row.get('url'))
        if not url:
            continue
        verified.append((row, url))
    if not verified:
        return ('<span class="coverage-pending" aria-disabled="true" '
                'title="尚未取得已核验的公众号原文，后续自动补充。">公众号待补充</span>', '')

    def preferred(item):
        row = item[0]
        published = parse_date(row.get('published_at'))
        checked = parse_date(row.get('verified_at'))
        return (reprint_available(row), row.get('body_status') == 'readable',
                published.timestamp() if published else 0, checked.timestamp() if checked else 0)

    verified.sort(key=preferred, reverse=True)
    unique = {}
    for row, url in verified:
        unique.setdefault(url, (row, url))
    verified = list(unique.values())

    def destination(row, url):
        if reprint_available(row):
            return root + 'wechat/' + article_name(row) + '.html', ''
        return url, ' target="_blank" rel="noopener noreferrer"'

    first, first_url = verified[0]
    href, attributes = destination(first, first_url)
    label = ' · '.join(str(value) for value in (
        first.get('title') or '公众号文章', first.get('account'),
        str(first.get('published_at') or '')[:10]) if value)
    if first.get('body_status') == 'challenge':
        label += ' · 如需访问验证，请在微信原文完成。'
    entry = (f'<a class="coverage-link" href="{escape(href, quote=True)}"{attributes} '
             f'title="{escape(label, quote=True)}" aria-label="公众号介绍：{escape(label, quote=True)}">公众号介绍</a>')
    if len(verified) == 1:
        return entry, ''

    identifier = 'paper-coverage-' + re.sub(r'[^a-zA-Z0-9_-]', '', str(paper_id) + '-' + suffix)
    entry += (f'<button type="button" class="text-button coverage-toggle coverage-more" aria-expanded="false" '
              f'aria-controls="{identifier}">更多来源<span aria-hidden="true">＋</span></button>')
    rows = []
    for row, url in verified:
        href, attributes = destination(row, url)
        date = str(row.get('published_at') or '')[:10]
        meta = escape(row.get('account') or '公众号名称待核实') + (' · ' + escape(date) if date else '')
        rows.append('<li><h4>' + f'<a href="{escape(href, quote=True)}"{attributes}>'
                    + escape(row.get('title') or '公众号文章') + '</a></h4>'
                    + '<p class="coverage-source">' + meta + '</p>'
                    + (f'<div class="coverage-actions"><a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">公众号原文</a></div>'
                       if reprint_available(row) else '') + '</li>')
    panel = (f'<section class="paper-coverage-panel" id="{identifier}" aria-label="已核实的公众号来源" hidden>'
             '<ul class="coverage-list">' + ''.join(rows) + '</ul></section>')
    return entry, panel


def render_reprint(row):
    from tools.build_site import document, safe_url
    body = []
    for block in row.get('blocks', []):
        kind = block.get('type')
        if kind in ('heading', 'paragraph'):
            tag = 'h2' if kind == 'heading' else 'p'
            body.append(f'<{tag}>' + escape(block.get('text', '')) + f'</{tag}>')
        elif kind == 'image' and re.fullmatch(r'assets/wechat/[a-f0-9]{32,64}\.(?:png|jpe?g|webp|gif)', block.get('src', '')):
            alt = escape(block.get('alt') or '原文配图')
            body.append(f'<figure class="article-original-figure"><a class="article-zoom" href="../{block["src"]}" aria-label="放大{alt}">'
                        f'<img src="../{block["src"]}" alt="{alt}" loading="lazy"></a>'
                        + (f'<figcaption>{escape(block["alt"])}</figcaption>' if block.get('alt') else '') + '</figure>')
    content = ('<article class="reading-article wechat-reprint"><a class="article-back" href="../">返回每日推荐</a>'
               + '<header class="article-header"><h1>' + escape(row['title']) + '</h1>'
               + '<p class="article-byline">' + escape(row.get('account', '')) + '</p>'
               + ('<p class="article-byline">' + escape(row['author']) + '</p>' if row.get('author') else '')
               + '<p class="article-updated">' + escape(str(row.get('published_at') or '')[:10]) + ' · 授权转载</p>'
               + f'<p><a href="{safe_url(row["url"])}" target="_blank" rel="noopener noreferrer">阅读公众号原文</a></p>'
               + ('<p class="article-source-note">' + escape(row['reprint_notice']) + '</p>' if row.get('reprint_notice') else '') + '</header>'
               + ''.join(body) + '<footer class="article-end"><p>本文转载自 ' + escape(row.get('account') or '原公众号')
               + '，内容及观点属于原作者。论文精读另行依据论文原文核验。</p></footer></article>'
               + '<dialog class="article-figure-dialog"><form method="dialog"><button type="submit">关闭大图</button></form><img alt="原文配图放大查看"></dialog>')
    return document(content, title=row['title'] + ' | 公众号介绍', root='../', active='reading')


def build_coverage(root, out, coverage=None):
    from src.paper_coverage import load_public, publish_assets
    coverage = deepcopy(load_public(root) if coverage is None else coverage)
    available = publish_assets(root, out) or set()
    for record in coverage.values():
        for article in record.get('articles', []):
            if 'blocks' in article:
                if any(b.get('type') == 'image' and b.get('src') not in available for b in article['blocks']):
                    article['reprint_notice'] = '部分原文配图暂不可用，请通过公众号原文查看完整图文。'
                    article['reprint_complete'] = False
                article['blocks'] = [b for b in article['blocks'] if b.get('type') != 'image' or b.get('src') in available]
    directory = out / 'wechat'
    directory.mkdir(parents=True, exist_ok=True)
    articles = {article_name(a): a for record in coverage.values() for a in record.get('articles', [])
                if a.get('match_status') == 'verified' and reprint_available(a)}
    for path in directory.glob('*.html'):
        if re.fullmatch(r'[a-f0-9]{24}', path.stem) and path.stem not in articles and path.resolve().parent == directory.resolve():
            path.unlink()
    for name, article in articles.items():
        (directory / (name + '.html')).write_text(render_reprint(article), encoding='utf-8')
    (out / 'paper-coverage.json').write_text(json.dumps({'version': 2, 'papers': coverage}, ensure_ascii=False, indent=2), encoding='utf-8')
