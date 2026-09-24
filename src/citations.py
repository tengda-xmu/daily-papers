"""GB/T 7714-2025 references for search results, with explicit metadata gaps.

Supported templates: journal articles, proceedings papers, preprints and web
articles. This is not a replacement for a full reference manager's CSL engine.
Never infer volume/page numbers or silently treat search snippets as journals.
"""
from datetime import date
import re
from urllib.parse import urlsplit


def safe_link(value):
    try:
        p = urlsplit(str(value or ''))
        return str(value) if p.scheme in ('https', 'http') and p.hostname and not p.username and not p.password else ''
    except ValueError:
        return ''


def plain(value):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', str(value or ''))).strip()


def reference(paper, accessed=None):
    b = (paper.get('raw_metadata') or {}).get('bibliography') or {}
    notes = []
    authors = []
    for author in b.get('authors') or []:
        if author.get('family'):
            family = plain(author['family'])
            given = plain(author.get('given', ''))
            if re.search(r'[\u3400-\u9fff]', family):
                authors.append(family + given)
            else:
                initials = ' '.join(x[0].upper() for x in re.findall(r'[^\W\d_]+', given))
                authors.append(' '.join(x for x in (family, initials) if x))
        elif author.get('name'):
            authors.append(plain(author['name']))
    if not authors:
        # String-only names are ambiguous (e.g. Wei Wang vs Wang Wei).
        # Preserve them rather than inventing a family/given name split.
        authors = [plain(a) for a in paper.get('authors', []) if plain(a)]
        if authors and any(re.search(r'[A-Za-z]', a) for a in authors):
            notes.append('作者姓名未提供姓/名结构，保留来源写法；投稿前核对姓名缩写')
    if not authors:
        notes.append('来源未提供作者')
    zh = bool(re.search(r'[\u3400-\u9fff]', paper.get('title', '')))
    names = ', '.join(authors[:3]) + ((', 等' if zh else ', et al.') if len(authors) > 3 else '')
    title = plain(paper.get('title'))
    url = safe_link(paper.get('landing_url'))
    doi = plain(paper.get('doi'))
    published = plain(paper.get('published_at'))[:10]
    year = published[:4] if re.match(r'^\d{4}', published) else ''
    if not year:
        notes.append('来源未提供出版年')
    kind = b.get('type', '')
    sources = (paper.get('raw_metadata') or {}).get('sources', [])
    is_preprint = kind == 'posted-content' or (('arXiv' in sources or paper.get('source') == 'arXiv') and not paper.get('venue'))
    venue = plain(paper.get('venue'))
    if paper.get('source') == 'Google Scholar' and not kind:
        venue = ''  # Its venue field contains a mixed author/year/site snippet.
    if kind == 'proceedings-article':
        marker = 'C'
    elif is_preprint:
        marker = 'PP'
    elif paper.get('source') == '微信公众号':
        marker = 'EB'
    elif kind == 'journal-article' or (venue and not kind):
        marker = 'J'
        if not kind:
            notes.append('文献类型按期刊字段整理，请核对')
    else:
        marker = 'EB'
        notes.append('文献类型未确定，暂按网络文献著录')
    online = bool(url or doi)
    head = (names.rstrip('. ') + '. ' if names else '') + title + '[' + marker + ('/OL' if online else '') + ']'
    volume, issue, page = (plain(b.get(k)) for k in ('volume', 'issue', 'page'))
    if marker == 'J':
        if not venue:
            notes.append('缺期刊名')
        if not volume or not page:
            notes.append('卷期或页码/文章号不全，可能为在线优先出版')
        details = ', '.join(x for x in (venue, year if volume or issue else published, volume) if x)
        details += '(' + issue + ')' if issue else ''
        details += ': ' + page if page else ''
        text = head + ('. ' + details if details else '')
    elif marker == 'C':
        publisher = plain(b.get('publisher'))
        text = head + ('// ' + venue if venue else '')
        text += '. ' + ', '.join(x for x in (publisher, year) if x)
        text += ': ' + page if page else ''
        notes.append('会议出版地、编者等信息请对照原文补全')
    elif marker == 'PP':
        publisher = plain(b.get('publisher')) or ('arXiv' if 'arxiv' in url else '')
        text = head + ('. ' + publisher if publisher else '')
        if published:
            text += ' (' + published + ')'
    else:
        text = head + ('. ' + venue if venue else '')
        if published:
            text += ' (' + published + ')'
    if online:
        text += '[' + str(accessed or date.today()) + ']'
    text = text.rstrip('. ') + '.'
    if url and not (doi and url.rstrip('/').casefold() == 'https://doi.org/' + doi.casefold()):
        text += ' ' + url.rstrip('.') + '.'
    if doi:
        text += ' DOI:' + doi + '.'
    return {'text': text, 'notes': notes, 'standard': 'GB/T 7714-2025'}
