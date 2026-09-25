"""Collect DOI-verified, openly licensed original figures for current recommendations.

Failures are recorded per paper and never block publication. Existing images are
reused; failed attempts are retried on the next day, not on every site rebuild.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
from zipfile import ZipFile

from bs4 import BeautifulSoup
from PIL import Image
import requests

from src.models import normalize_doi
from src.paper_sources import HOSTS as ARTICLE_HOSTS, discover, page_identity

ROOT = Path(__file__).resolve().parents[1]
HOSTS = ARTICLE_HOSTS
CC = re.compile(r'https?://creativecommons\.org/licenses/(by(?:-nc)?(?:-nd|-sa)?)/(4\.0|3\.0)/?$', re.I)
EXCLUDED_CREDIT = re.compile(r'biorender|reprinted|reproduced (?:from|with)|used (?:with|by) permission|all rights reserved|not (?:covered|included)', re.I)


class Unavailable(Exception):
    def __init__(self, state):
        self.state = state


def read(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'daily-papers/1.0 (public research figures)'

    def __call__(self, url, limit=8_000_000):
        # Publisher redirects and external image links are checked before use.
        deadline = time.monotonic() + 45
        for _ in range(5):
            parsed = urlsplit(url)
            # Nature's public pages establish an anonymous cookie through these
            # two redirects, including when serving open-access manuscript PDFs.
            cookie_redirect = parsed.hostname == 'idp.nature.com' and parsed.path in ('/authorize', '/transit')
            if parsed.scheme != 'https' or (parsed.hostname not in HOSTS and not cookie_redirect) or parsed.port not in (None, 443) or parsed.username:
                raise Unavailable('unavailable')
            with self.session.get(url, timeout=(8, 20), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get('Location', ''))
                    continue
                if response.status_code in (401, 403, 429):
                    raise Unavailable('restricted')
                if response.status_code == 404:
                    raise Unavailable('not_found')
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > limit or time.monotonic() > deadline:
                        raise Unavailable('unavailable')
                    chunks.append(chunk)
                self.last_url = url
                return b''.join(chunks)
        raise Unavailable('unavailable')


def license_info(urls):
    for url in urls:
        match = CC.fullmatch(str(url).strip())
        if match:
            kind, version = match.groups()
            return {'license': f'CC {kind.upper()} {version}',
                    'license_url': f'https://creativecommons.org/licenses/{kind.lower()}/{version}/'}
    raise Unavailable('license_unconfirmed')


def plain(node):
    return node.get_text(' ', strip=True) if node else ''


def figure_priority(title):
    """Prefer a method overview to an isolated result or parameter sweep."""
    return -sum(bool(re.search(pattern, title, re.I)) for pattern in (
        r'framework|workflow|pipeline|overview', r'schematic|architecture|block diagram',
        r'proposed|method|approach|process'))


def nature_figure(doi, fetch):
    url = 'https://www.nature.com/articles/' + doi.split('/', 1)[1]
    soup = BeautifulSoup(fetch(url), 'html.parser')
    identity = soup.select_one('meta[name="citation_doi"]')
    if not identity or normalize_doi(identity.get('content', '')) != doi:
        raise Unavailable('unavailable')
    rights = soup.select_one('#rightslink')
    if rights:
        rights = rights.find_parent('section') or rights.parent
    # Limit the license search to this article's Rights and permissions section.
    if not rights:
        heading = soup.find(id=re.compile(r'^rights', re.I))
        rights = (heading.find_parent('section') or heading.parent) if heading else None
    if not rights:
        raise Unavailable('license_unconfirmed')
    licensing = license_info([a.get('href') for a in rights.select('a[href]')
                              if 'creativecommons.org/licenses/' in a.get('href', '')])
    authors = [m.get('content', '') for m in soup.select('meta[name="citation_author"]')]
    if not authors:
        raise Unavailable('unavailable')
    common = {**licensing, 'credit': authors[0] + (' et al.' if len(authors) > 1 else ''),
              'license_source': url}
    figures = sorted(soup.select('figure'), key=lambda f: figure_priority(plain(f.select_one('figcaption'))))
    for figure in figures:
        caption = figure.select_one('figcaption')
        label = re.match(r'(Fig\.\s*\d+)', plain(caption))
        image = figure.select_one('img[src]')
        if not label or not image or EXCLUDED_CREDIT.search(plain(figure)):
            continue
        image_url = urljoin(url, image['src'])
        # Reject related-article thumbnails, logos and images from another DOI.
        if urlsplit(image_url).hostname != 'media.springernature.com' or doi not in unquote(image_url).lower():
            continue
        image_url = re.sub(r'/lw\d+/', '/full/', image_url).split('?', 1)[0]
        number = re.search(r'\d+', label[1])[0]
        description = figure.select_one('.c-article-section__figure-description')
        return {**common, 'figure_label': label[1], 'title': plain(caption)[label.end():].lstrip(': '),
                'caption': plain(description) or plain(caption),
                'source_url': url + '/figures/' + number, 'source_image_url': image_url}, fetch(image_url, 20_000_000)
    # Accepted manuscripts may have figures only in their openly licensed PDF.
    pdf = next((urljoin(url, a['href']) for a in soup.select('a[href]')
                if a['href'].endswith('.pdf') and doi.split('/', 1)[1] in a['href']), None)
    if pdf:
        return pdf_figure(doi, pdf, common, fetch(pdf, 30_000_000))
    raise Unavailable('not_found')


def pdf_figure(doi, url, common, data):
    import fitz
    if not data.startswith(b'%PDF'):
        raise Unavailable('unavailable')
    with fitz.open(stream=data, filetype='pdf') as document:
        if doi not in ''.join(p.get_text().lower() for p in list(document)[:2]):
            raise Unavailable('unavailable')
        candidates = []
        for index, page in enumerate(document):
            blocks = [b for b in page.get_text('blocks') if b[6] == 0]
            captions = [b for b in blocks if re.match(r'^Fig(?:ure)?\.?\s*\d+\s*[|:.]', b[4].strip())]
            images = [im for im in page.get_images(full=True) if im[2] >= 400 and im[3] >= 250]
            # Associate one unambiguous caption with the neighbouring figure,
            # on either side. Page banners are never figure candidates.
            if len(captions) != 1:
                continue
            text = ' '.join(captions[0][4].split())
            if EXCLUDED_CREDIT.search(text):
                continue
            label = re.match(r'^(Fig(?:ure)?\.?\s*\d+)', text)[1]
            caption = fitz.Rect(captions[0][:4])
            margin = max(45, page.rect.height * .05)
            rects = [r for im in images for r in page.get_image_rects(im[0])]
            rects += [d['rect'] for d in page.get_drawings()]
            rects = [r for r in rects if r.y0 >= margin and r.width >= 80 and r.height >= 50
                     and (r.y1 <= caption.y1 or r.y0 >= caption.y0)]
            if not rects:
                continue
            distance = lambda r: max(caption.y0 - r.y1, r.y0 - caption.y1, 0)
            nearest = min(rects, key=distance)
            if distance(nearest) > 100:
                continue
            above = nearest.y1 <= caption.y1
            region = fitz.Rect(nearest)
            neighbours = [r for r in rects if (r.y1 <= caption.y1) == above]
            while True:
                before = tuple(region)
                for rect in neighbours:
                    expanded = fitz.Rect(region.x0 - 45, region.y0 - 45, region.x1 + 45, region.y1 + 45)
                    if expanded.intersects(rect):
                        region |= rect
                if tuple(region) == before:
                    break
            region = fitz.Rect(max(0, region.x0 - 30), max(margin, region.y0 - 30) if above else max(caption.y1, region.y0 - 10),
                               min(page.rect.width, region.x1 + 30), caption.y0 if above else min(page.rect.height - 30, region.y1 + 15))
            if region.width < 150 or region.height < 80:
                continue
            # Rasterize the original figure region, including vector/text labels
            # and all panels; never redraw a chart or publish the source PDF.
            extracted = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=region, alpha=False).tobytes('png')
            metadata = {**common, 'figure_label': label, 'title': '原文配图', 'caption': text,
                    'source_url': url + f'#page={index + 1}', 'source_image_url': url,
                    'pdf_page': index + 1, 'extraction': 'original_figure_region', 'region': list(region)}
            candidates.append((figure_priority(text), index, metadata, extracted))
        if candidates:
            best = min(candidates, key=lambda value: value[:2])
            return best[2], best[3]
    raise Unavailable('not_found')


def publisher_figure(doi, paper, fetch):
    """Publisher-declared figures/PDFs for Elsevier, ASME and known OA hosts."""
    from src.paper_sources import allowed
    def get(url, limit=20_000_000):
        body = fetch(url, limit)
        return body, getattr(fetch, 'last_url', url)
    discovery = discover({**paper, 'doi': doi}, get)
    state = 'not_found'
    for url in discovery['urls'][:8]:
        try:
            body, final = get(url)
            if body.startswith(b'%PDF'):
                continue  # A PDF URL alone does not establish image permissions.
            soup = BeautifulSoup(body, 'html.parser')
            if not page_identity(soup, {**paper, 'doi': doi}):
                continue
            rights = soup.select('.Copyright, .copyright, .license, #rightslink, .permissions, #permissions')
            links = [a.get('href') for block in rights for a in block.select('a[href]')]
            licensing = license_info([urljoin(final, link) for link in links if link])
            authors = [m.get('content', '') for m in soup.select('meta[name="citation_author"]')] or paper.get('authors', [])
            if not authors:
                continue
            common = {**licensing, 'credit': str(authors[0]) + (' et al.' if len(authors) > 1 else ''), 'license_source': final}
            figures = soup.select('figure, div.fig, .fig-section')
            for figure in sorted(figures, key=lambda n: figure_priority(plain(n))):
                caption = figure.select_one('figcaption, .caption, .fig-caption, .fig_caption')
                label = re.match(r'^(Fig(?:ure)?\.?\s*\d+)', plain(caption), re.I)
                image = figure.select_one('img[src],img[data-src]')
                if not label or not image or EXCLUDED_CREDIT.search(plain(figure)):
                    continue
                image_url = urljoin(final, image.get('data-src') or image.get('src'))
                larger = next((urljoin(final, a['href']) for a in figure.select('a[href]')
                               if re.search(r'\.(?:png|jpe?g)(?:\?|$)', a['href'], re.I)), None)
                image_url = larger or image_url
                if not allowed(image_url):
                    continue
                return {**common, 'figure_label': label[1], 'title': plain(caption)[label.end():].lstrip('.: '),
                        'caption': plain(caption), 'source_url': final + ('#' + figure['id'] if figure.get('id') else ''),
                        'source_image_url': image_url}, fetch(image_url, 20_000_000)
            pdfs = [urljoin(final, n.get('content', '')) for n in soup.select('meta[name="citation_pdf_url"]')]
            pdfs += [urljoin(final, n['href']) for n in soup.select('a[href]') if re.search(r'\.pdf(?:\?|$)', n['href'])]
            for pdf in list(dict.fromkeys(pdfs))[:2]:
                if allowed(pdf):
                    return pdf_figure(doi, pdf, common, fetch(pdf, 30_000_000))
        except Unavailable as exc:
            state = exc.state
        except Exception:
            state = 'unavailable'
    raise Unavailable(state)


def pmc_figure(doi, fetch):
    base = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'
    result = json.loads(fetch(base + 'search?format=json&query=' + quote('DOI:' + doi)))
    rows = result.get('resultList', {}).get('result', [])
    pmcid = next((r.get('pmcid', '') for r in rows if normalize_doi(r.get('doi', '')) == doi), '')
    if not re.fullmatch(r'PMC\d+', pmcid):
        raise Unavailable('not_found')
    from xml.etree import ElementTree as ET
    root = ET.fromstring(fetch(base + pmcid + '/fullTextXML'))
    if not any(normalize_doi(n.text or '') == doi for n in root.findall('./front/article-meta/article-id[@pub-id-type="doi"]')):
        raise Unavailable('unavailable')
    permissions = root.find('./front/article-meta/permissions')
    licensing = license_info(re.findall(r'https?://creativecommons.org/licenses/[a-z-]+/[\d.]+/',
                                        ET.tostring(permissions, encoding='unicode') if permissions is not None else ''))
    author = root.find('./front/article-meta/contrib-group/contrib/name')
    credit = ' '.join(author.itertext()) if author is not None else ''
    if not credit.strip():
        raise Unavailable('unavailable')
    for fig in root.findall('.//body//fig'):
        caption = fig.find('caption')
        graphic = fig.find('graphic')
        if caption is None or graphic is None or EXCLUDED_CREDIT.search(' '.join(fig.itertext())):
            continue
        name = graphic.get('{http://www.w3.org/1999/xlink}href', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:png|jpg|jpeg)', name):
            continue
        bundle_url = base + pmcid + '/supplementaryFiles'
        with ZipFile(BytesIO(fetch(bundle_url, 100_000_000))) as bundle:
            # Read only the matched figure, never unpack the archive onto disk.
            candidates = [i for i in bundle.infolist() if i.filename == name]
            if len(candidates) != 1 or candidates[0].file_size > 20_000_000:
                raise Unavailable('not_found')
            image = bundle.read(candidates[0])
        return {**licensing, 'credit': credit.strip() + ' et al.',
                'figure_label': fig.findtext('label', 'Figure').rstrip('.'),
                'title': ''.join(caption.find('title').itertext()) if caption.find('title') is not None else '原文配图',
                'caption': ' '.join(caption.itertext()), 'license_source': base + pmcid + '/fullTextXML',
                'source_url': 'https://pmc.ncbi.nlm.nih.gov/articles/' + pmcid + '/#' + fig.get('id', ''),
                'source_image_url': bundle_url, 'archive_entry': name}, image
    raise Unavailable('not_found')


def image_info(data):
    with Image.open(BytesIO(data)) as image:
        if image.format not in ('PNG', 'JPEG') or min(image.size) < 200 or image.width * image.height > 40_000_000:
            raise Unavailable('unavailable')
        result = (image.width, image.height, 'png' if image.format == 'PNG' else 'jpg')
        image.verify()
        return result


def collect(root=ROOT, fetch=None, now=None, *, retry=False):
    from src.figures import get_figure
    from src.paper_identity import paper_doi
    fetch = fetch or Fetcher()
    now = now or datetime.now(timezone.utc)
    path = root / 'data/figures/catalog.json'
    catalog = read(path)
    entries, checks = catalog.setdefault('entries', {}), catalog.setdefault('checks', {})
    payload = read(root / 'data/daily.json')
    papers = payload.get('core', [])
    from src.editions import entries as editions, relative_path
    for edition in reversed(editions(root / 'data')):
        papers = papers + read(root / 'data' / relative_path(edition)).get('core', [])
    counts = {'saved': 0, 'existing': 0, 'unavailable': 0}
    seen = set()
    started, attempts = time.monotonic(), 0
    for paper in papers:
        if attempts >= 20 or time.monotonic() - started > 400:
            break
        doi = paper_doi(paper) if root == ROOT else normalize_doi(paper.get('doi', ''))
        if not doi or doi in seen:
            continue
        seen.add(doi)
        old = entries.get(doi, {})
        filename = old.get('image_path', '').removeprefix('assets/figures/')
        if (root == ROOT and get_figure(doi)) or (re.fullmatch(r'auto-[a-f0-9-]+\.(?:png|jpg)', filename or '') and (path.parent / 'images' / filename).is_file()):
            counts['existing'] += 1
            continue
        try:
            last = datetime.fromisoformat(checks.get(doi, {}).get('checked_at', ''))
            if not retry and 0 <= (now - last).total_seconds() < 86400:
                counts['unavailable'] += 1
                continue
        except (ValueError, TypeError):
            pass
        state = 'unavailable'
        attempts += 1
        try:
            if doi.startswith('10.1038/'):
                metadata, data = nature_figure(doi, fetch)
            else:
                try:
                    metadata, data = publisher_figure(doi, paper, fetch)
                except Unavailable as publisher_error:
                    try:
                        metadata, data = pmc_figure(doi, fetch)
                    except Unavailable:
                        raise publisher_error
            width, height, extension = image_info(data)
            digest = hashlib.sha256(data).hexdigest()
            filename = 'auto-' + hashlib.sha256(doi.encode()).hexdigest()[:16] + '-' + digest[:12] + '.' + extension
            image_path = path.parent / 'images' / filename
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(data)
            entries[doi] = {**metadata, 'image_path': 'assets/figures/' + filename, 'width': width, 'height': height,
                            'sha256': digest, 'modified': False, 'verified_at': now.isoformat()}
            state = 'ready'
            counts['saved'] += 1
        except Unavailable as exc:
            state = exc.state
            counts['unavailable'] += 1
        except Exception:
            # Malformed publisher HTML/XML/images must not cancel the edition.
            counts['unavailable'] += 1
        checks[doi] = {'state': state, 'checked_at': now.isoformat()}
        write(path, catalog)
    return counts


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry', action='store_true', help='Retry unavailable figures now; existing images are reused')
    print(json.dumps(collect(retry=parser.parse_args().retry), ensure_ascii=False))
