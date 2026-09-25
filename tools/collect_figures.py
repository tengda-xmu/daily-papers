"""Collect DOI-verified, openly licensed original figures for the current core papers.

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

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {'www.nature.com', 'media.springernature.com', 'www.ebi.ac.uk'}
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
            if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.port not in (None, 443) or parsed.username:
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
    for figure in soup.select('figure'):
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
        for index, page in enumerate(document):
            blocks = [b for b in page.get_text('blocks') if b[6] == 0]
            captions = [b for b in blocks if re.match(r'^Fig(?:ure)?\.?\s*\d+\s*[|:.]', b[4].strip())]
            images = [im for im in page.get_images(full=True) if im[2] >= 400 and im[3] >= 250]
            # Multiple images, masks, or ambiguous captions need manual review.
            if len(captions) != 1 or len(images) != 1 or images[0][1] != 0:
                continue
            text = ' '.join(captions[0][4].split())
            if EXCLUDED_CREDIT.search(text):
                continue
            label = re.match(r'^(Fig(?:ure)?\.?\s*\d+)', text)[1]
            rects = page.get_image_rects(images[0][0])
            if len(rects) != 1 or rects[0].y0 > captions[0][3]:
                continue
            extracted = document.extract_image(images[0][0])
            return {**common, 'figure_label': label, 'title': '原文配图', 'caption': text,
                    'source_url': url + f'#page={index + 1}', 'source_image_url': url,
                    'pdf_page': index + 1, 'extraction': 'embedded_image'}, extracted['image']
    raise Unavailable('not_found')


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


def collect(root=ROOT, fetch=None, now=None):
    from src.figures import get_figure
    fetch = fetch or Fetcher()
    now = now or datetime.now(timezone.utc)
    path = root / 'data/figures/catalog.json'
    catalog = read(path)
    entries, checks = catalog.setdefault('entries', {}), catalog.setdefault('checks', {})
    papers = read(root / 'data/daily.json').get('core', [])
    counts = {'saved': 0, 'existing': 0, 'unavailable': 0}
    for paper in papers[:10]:
        doi = normalize_doi(paper.get('doi', ''))
        if not doi:
            continue
        old = entries.get(doi, {})
        filename = old.get('image_path', '').removeprefix('assets/figures/')
        if (root == ROOT and get_figure(doi)) or (re.fullmatch(r'auto-[a-f0-9-]+\.(?:png|jpg)', filename or '') and (path.parent / 'images' / filename).is_file()):
            counts['existing'] += 1
            continue
        try:
            last = datetime.fromisoformat(checks.get(doi, {}).get('checked_at', ''))
            if 0 <= (now - last).total_seconds() < 86400:
                counts['unavailable'] += 1
                continue
        except (ValueError, TypeError):
            pass
        state = 'unavailable'
        try:
            metadata, data = nature_figure(doi, fetch) if doi.startswith('10.1038/') else pmc_figure(doi, fetch)
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
    print(json.dumps(collect(), ensure_ascii=False))
