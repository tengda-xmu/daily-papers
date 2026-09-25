"""DOI-matched public article discovery shared by reading and figure jobs."""
from html import unescape
import hashlib
import ipaddress
import json
import os
import re
import socket
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

from src.models import normalize_doi
from src.paper_identity import paper_doi

HOSTS = {'doi.org', 'api.crossref.org', 'api.openalex.org', 'api.semanticscholar.org', 'www.nature.com', 'nature.com',
         'media.springernature.com', 'www.sciencedirect.com', 'www.cell.com', 'api.elsevier.com',
         'ars.els-cdn.com', 'asmedigitalcollection.asme.org', 'www.ebi.ac.uk',
         'pmc.ncbi.nlm.nih.gov', 'arxiv.org', 'export.arxiv.org', 'link.springer.com',
         'www.frontiersin.org', 'journals.plos.org', 'www.science.org'}


def allowed(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and p.port in (None, 443) and not p.username and not p.password
            and (p.hostname in HOSTS or (p.hostname == 'idp.nature.com' and p.path in ('/authorize', '/transit'))))


class PublicFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'DailyPapers/1.0 (public scholarly reading)'

    def __call__(self, url, limit=20_000_000):
        for _ in range(6):
            if not allowed(url):
                raise ValueError('Unsupported public article host')
            host = urlsplit(url).hostname
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError('Non-public article address')
            headers = {}
            if host == 'api.elsevier.com' and os.getenv('ELSEVIER_API_KEY'):
                headers['X-ELS-APIKey'] = os.environ['ELSEVIER_API_KEY']
            if host == 'api.semanticscholar.org' and os.getenv('SEMANTIC_SCHOLAR_API_KEY'):
                headers['x-api-key'] = os.environ['SEMANTIC_SCHOLAR_API_KEY']
            with self.session.get(url, headers=headers, timeout=(8, 20), stream=True, allow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['Location'])
                    continue
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_content(65536):
                    content.extend(chunk)
                    if len(content) > limit:
                        raise ValueError('Article exceeds download limit')
                return bytes(content), url
        raise ValueError('Too many article redirects')


def normalized_title(text):
    return re.sub(r'[^\w]+', '', unescape(str(text)).casefold())


def matches(text, paper):
    doi = paper_doi(paper)
    title = normalized_title(paper.get('title', ''))
    return bool((doi and doi in text.casefold()) or (len(title) > 25 and title in normalized_title(text)))


def page_identity(soup, paper):
    identities = ' '.join(m.get('content', '') for m in soup.select(
        'meta[name="citation_doi"],meta[name="dc.Identifier"],meta[name="DC.Identifier"],'
        'meta[name="prism.doi"],meta[name="citation_title"],meta[property="og:title"]'))
    heading = soup.select_one('h1')
    return matches(identities, paper) or (heading is not None and matches(heading.get_text(' ', strip=True), paper))


def clean_abstract(text):
    text = ' '.join(BeautifulSoup(str(text or ''), 'html.parser').get_text(' ', strip=True).split())
    text = re.sub(r'^Abstract\s*', '', text, flags=re.I)
    # Search teasers are not complete abstracts, even when longer than 80 chars.
    return text if len(text) >= 120 and '…' not in text and '...' not in text else ''


def discover(paper, fetch=None):
    fetch = fetch or PublicFetcher()
    doi = paper_doi(paper)
    result = {'doi': doi, 'abstract': '', 'abstract_url': '', 'urls': [], 'errors': []}
    def add(url):
        if isinstance(url, str) and allowed(url) and url not in result['urls']:
            result['urls'].append(url)
    for key in ('oa_url', 'pdf_url', 'landing_url'):
        add(paper.get(key))
    if doi:
        try:
            body, _ = fetch('https://api.crossref.org/works/' + quote(doi, safe=''))
            row = json.loads(body)['message']
            if normalize_doi(row.get('DOI')) == doi:
                abstract = clean_abstract(row.get('abstract'))
                if abstract:
                    result.update(abstract=abstract, abstract_url='https://doi.org/' + doi)
                add(row.get('resource', {}).get('primary', {}).get('URL'))
                for link in row.get('link', []):
                    if link.get('content-type') in ('application/pdf', 'text/xml', 'text/html'):
                        add(link.get('URL'))
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException):
            result['errors'].append('crossref_unavailable')
        add('https://doi.org/' + doi)
        if doi.startswith('10.1038/'):
            add('https://www.nature.com/articles/' + doi.split('/', 1)[1])
            add('https://www.nature.com/articles/' + doi.split('/', 1)[1] + '.pdf')
        if doi.startswith('10.1016/'):
            add('https://api.elsevier.com/content/article/doi/' + quote(doi, safe='/'))
        if doi.startswith('10.48550/arxiv.'):
            add('https://arxiv.org/pdf/' + doi.split('arxiv.', 1)[1])
        try:
            url = 'https://api.openalex.org/works/https://doi.org/' + quote(doi, safe='/')
            if os.getenv('OPENALEX_API_KEY'):
                url += '?api_key=' + quote(os.environ['OPENALEX_API_KEY'])
            body, _ = fetch(url)
            row = json.loads(body)
            if normalize_doi(row.get('doi')) == doi:
                if not result['abstract'] and isinstance(row.get('abstract_inverted_index'), dict):
                    words = {i: word for word, positions in row['abstract_inverted_index'].items() for i in positions}
                    result['abstract'] = clean_abstract(' '.join(words[k] for k in sorted(words)))
                    result['abstract_url'] = 'https://doi.org/' + doi
                for location in [row.get('best_oa_location') or {}, *(row.get('locations') or [])]:
                    if location.get('is_oa'):
                        add(location.get('pdf_url')); add(location.get('landing_page_url'))
        except (OSError, ValueError, KeyError, TypeError, requests.RequestException):
            result['errors'].append('openalex_unavailable')
        if not result['abstract']:
            try:
                body, _ = fetch('https://api.semanticscholar.org/graph/v1/paper/DOI:' + quote(doi, safe='/')
                                + '?fields=title,abstract,externalIds,openAccessPdf')
                row = json.loads(body)
                if normalize_doi((row.get('externalIds') or {}).get('DOI')) == doi:
                    result['abstract'] = clean_abstract(row.get('abstract'))
                    result['abstract_url'] = 'https://doi.org/' + doi
                    add((row.get('openAccessPdf') or {}).get('url'))
            except (OSError, ValueError, KeyError, TypeError, requests.RequestException):
                result['errors'].append('semantic_scholar_unavailable')
    return result


def inspect_html(body, url, paper):
    soup = BeautifulSoup(body, 'html.parser')
    if not page_identity(soup, paper):
        raise ValueError('Article identity mismatch')
    abstract = ''
    for node in soup.select('#Abs1, section#abstract, .abstract, #abstract, meta[name="citation_abstract"]'):
        abstract = clean_abstract(node.get('content') or node.get_text(' ', strip=True))
        if abstract:
            break
    links = [urljoin(url, n.get('content', '')) for n in soup.select('meta[name="citation_pdf_url"]')]
    links += [urljoin(url, n['href']) for n in soup.select('a[href]') if re.search(r'\.pdf(?:\?|$)', n['href'])
              and not re.search(r'supplement|suppl|esm|mmc\d', n['href'], re.I)]
    for n in soup.select('script,style,nav,footer,aside,.references,#references,.related-articles'):
        n.decompose()
    article = soup.select_one('article') or soup.select_one('main')
    parts = []
    if article:
        for n in article.select('h2,h3,p,figcaption'):
            text = n.get_text(' ', strip=True)
            if text and text not in parts:
                parts.append(text)
    text = '\n'.join(parts)
    headings = ' '.join(n.get_text(' ', strip=True) for n in article.select('h2,h3')) if article else ''
    complete = len(text) >= 6000 and len(set(re.findall(r'\b(methods|methodology|results|discussion|conclusions?)\b', headings, re.I))) >= 2
    document = {'kind': 'html', 'url': url, 'name': '原文网页', 'hash': hashlib.sha256(body).hexdigest()[:16],
                'page_count': 0, 'scan_pages': 0,
                'pages': [{'label': f'S{i+1}', 'text': p, 'scan': False} for i, p in enumerate(parts)]} if complete else None
    return abstract, document, [link for link in links if allowed(link)]


def inspect_xml(body, url, paper):
    from xml.etree import ElementTree as ET
    try:
        tree = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError('Invalid article XML') from exc
    tag = lambda n: n.tag.rsplit('}', 1)[-1]
    text = lambda n: ' '.join(' '.join(n.itertext()).split()) if n is not None else ''
    first = lambda names: next((n for n in tree.iter() if tag(n) in names), None)
    identity = text(first({'doi'})) + ' ' + text(first({'article-title', 'title'}))
    if not matches(identity, paper):
        raise ValueError('Article XML identity mismatch')
    abstract = clean_abstract(text(first({'abstract', 'description'})))
    body_node = first({'body'})
    paragraphs = [text(n) for n in body_node.iter() if tag(n) in ('para', 'p', 'section-title', 'title')] if body_node is not None else []
    complete = sum(map(len, paragraphs)) >= 6000
    doc = {'kind': 'html', 'url': url, 'name': '原文 XML', 'hash': hashlib.sha256(body).hexdigest()[:16],
           'page_count': 0, 'scan_pages': 0,
           'pages': [{'label': f'S{i+1}', 'text': t, 'scan': False} for i, t in enumerate(paragraphs)]} if complete else None
    return abstract, doc
