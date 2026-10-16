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
import tempfile
from urllib.parse import quote, unquote, urljoin, urlsplit
from zipfile import ZipFile

from bs4 import BeautifulSoup
from PIL import Image
import requests

from src.models import normalize_doi
from src.paper_sources import HOSTS as ARTICLE_HOSTS, discover, page_identity
from src.conferences import public_url as conference_url

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
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.stem + '-', suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    try:
        # On Windows an active image/catalog reader can briefly block replace.
        for attempt in range(12):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 11:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


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
            if parsed.scheme != 'https' or (parsed.hostname not in HOSTS and not cookie_redirect and not conference_url(url)) or parsed.port not in (None, 443) or parsed.username:
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
    return nature_figures(doi, fetch, maximum=1)[0]


def nature_figures(doi, fetch, *, maximum=12):
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
    collected = []
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
        metadata = {**common, 'figure_label': label[1], 'title': plain(caption)[label.end():].lstrip(': '),
                'caption': plain(description) or plain(caption),
                'source_url': url + '/figures/' + number, 'source_image_url': image_url}
        try:
            collected.append((metadata, fetch(image_url, 20_000_000)))
        except (Unavailable, requests.RequestException):
            continue
        if len(collected) >= maximum:
            break
    if collected:
        return collected
    # Accepted manuscripts may have figures only in their openly licensed PDF.
    pdf = next((urljoin(url, a['href']) for a in soup.select('a[href]')
                if a['href'].endswith('.pdf') and doi.split('/', 1)[1] in a['href']), None)
    if pdf:
        return pdf_figures(doi, pdf, common, fetch(pdf, 30_000_000), maximum=maximum)
    raise Unavailable('not_found')


def pdf_figure(doi, url, common, data, paper=None):
    return pdf_figures(doi, url, common, data, paper=paper, maximum=1)[0]


def pdf_figures(doi, url, common, data, paper=None, *, maximum=12):
    import fitz
    if not data.startswith(b'%PDF'):
        raise Unavailable('unavailable')
    with fitz.open(stream=data, filetype='pdf') as document:
        identity = ''.join(p.get_text().lower() for p in list(document)[:2])
        from src.paper_sources import matches
        if not ((doi and doi in identity) or (paper and matches(identity, paper))):
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
            metadata = {**common, 'figure_label': label, 'title': '原文配图', 'caption': text,
                    'source_url': url + f'#page={index + 1}', 'source_image_url': url,
                    'pdf_page': index + 1, 'extraction': 'original_figure_region', 'region': list(region)}
            candidates.append((figure_priority(text), index, metadata, region))
        if candidates:
            return [(item[2], document[item[1]].get_pixmap(matrix=fitz.Matrix(2, 2), clip=item[3], alpha=False).tobytes('png'))
                    for item in sorted(candidates, key=lambda value: value[:2])[:maximum]]
    raise Unavailable('not_found')


def publisher_figure(doi, paper, fetch):
    return publisher_figures(doi, paper, fetch, maximum=1)[0]


def publisher_figures(doi, paper, fetch, *, maximum=12):
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
            collected, labels = [], set()
            for figure in sorted(figures, key=lambda n: figure_priority(plain(n))):
                caption = figure.select_one('figcaption, .caption, .fig-caption, .fig_caption')
                label = re.match(r'^(Fig(?:ure)?\.?\s*\d+)', plain(caption), re.I)
                image = figure.select_one('img[src],img[data-src]')
                if not label or not image or EXCLUDED_CREDIT.search(plain(figure)) or label[1] in labels:
                    continue
                image_url = urljoin(final, image.get('data-src') or image.get('src'))
                larger = next((urljoin(final, a['href']) for a in figure.select('a[href]')
                               if re.search(r'\.(?:png|jpe?g)(?:\?|$)', a['href'], re.I)), None)
                image_url = larger or image_url
                if not allowed(image_url):
                    continue
                metadata = {**common, 'figure_label': label[1], 'title': plain(caption)[label.end():].lstrip('.: '),
                        'caption': plain(caption), 'source_url': final + ('#' + figure['id'] if figure.get('id') else ''),
                        'source_image_url': image_url}
                try:
                    collected.append((metadata, fetch(image_url, 20_000_000)))
                    labels.add(label[1])
                except (Unavailable, requests.RequestException):
                    continue
                if len(collected) >= maximum:
                    break
            if collected:
                return collected
            pdfs = [urljoin(final, n.get('content', '')) for n in soup.select('meta[name="citation_pdf_url"]')]
            pdfs += [urljoin(final, n['href']) for n in soup.select('a[href]') if re.search(r'\.pdf(?:\?|$)', n['href'])]
            for pdf in list(dict.fromkeys(pdfs))[:2]:
                if allowed(pdf):
                    return pdf_figures(doi, pdf, common, fetch(pdf, 30_000_000), paper=paper, maximum=maximum)
        except Unavailable as exc:
            state = exc.state
        except Exception:
            state = 'unavailable'
    raise Unavailable(state)


def pmc_figure(doi, fetch):
    return pmc_figures(doi, fetch, maximum=1)[0]


def pmc_figures(doi, fetch, *, maximum=12):
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
    collected, archive = [], None
    for fig in root.findall('.//body//fig'):
        caption = fig.find('caption')
        graphic = fig.find('graphic')
        if caption is None or graphic is None or EXCLUDED_CREDIT.search(' '.join(fig.itertext())):
            continue
        name = graphic.get('{http://www.w3.org/1999/xlink}href', '')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:png|jpg|jpeg)', name):
            continue
        bundle_url = base + pmcid + '/supplementaryFiles'
        if archive is None:
            archive = fetch(bundle_url, 100_000_000)
        with ZipFile(BytesIO(archive)) as bundle:
            # Read only the matched figure, never unpack the archive onto disk.
            candidates = [i for i in bundle.infolist() if i.filename == name]
            if len(candidates) != 1 or candidates[0].file_size > 20_000_000:
                continue
            image = bundle.read(candidates[0])
        metadata = {**licensing, 'credit': credit.strip() + ' et al.',
                'figure_label': fig.findtext('label', 'Figure').rstrip('.'),
                'title': ''.join(caption.find('title').itertext()) if caption.find('title') is not None else '原文配图',
                'caption': ' '.join(caption.itertext()), 'license_source': base + pmcid + '/fullTextXML',
                'source_url': 'https://pmc.ncbi.nlm.nih.gov/articles/' + pmcid + '/#' + fig.get('id', ''),
                'source_image_url': bundle_url, 'archive_entry': name}
        collected.append((metadata, image))
        if len(collected) >= maximum:
            break
    if collected:
        return collected
    raise Unavailable('not_found')


def image_info(data):
    with Image.open(BytesIO(data)) as image:
        if image.format not in ('PNG', 'JPEG') or min(image.size) < 200 or image.width * image.height > 40_000_000:
            raise Unavailable('unavailable')
        result = (image.width, image.height, 'png' if image.format == 'PNG' else 'jpg')
        image.verify()
        return result


def collect(root=ROOT, fetch=None, now=None, *, retry=False, time_budget=400):
    from src.figures import get_figure, figure_key
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
        if attempts >= 20 or time.monotonic() - started > time_budget:
            break
        doi = paper_doi(paper) if root == ROOT else normalize_doi(paper.get('doi', ''))
        key = figure_key(paper) if root == ROOT or not doi else doi
        if not key or key in seen:
            continue
        seen.add(key)
        old = entries.get(key, {})
        filename = old.get('image_path', '').removeprefix('assets/figures/')
        if (root == ROOT and get_figure(key)) or (re.fullmatch(r'auto-[a-f0-9-]+\.(?:png|jpg)', filename or '') and (path.parent / 'images' / filename).is_file()):
            counts['existing'] += 1
            continue
        try:
            last = datetime.fromisoformat(checks.get(key, {}).get('checked_at', ''))
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
                    if not doi:
                        raise publisher_error
                    try:
                        metadata, data = pmc_figure(doi, fetch)
                    except Unavailable:
                        raise publisher_error
            width, height, extension = image_info(data)
            digest = hashlib.sha256(data).hexdigest()
            filename = 'auto-' + hashlib.sha256(key.encode()).hexdigest()[:16] + '-' + digest[:12] + '.' + extension
            image_path = path.parent / 'images' / filename
            image_path.parent.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(data)
            entries[key] = {**metadata, 'image_path': 'assets/figures/' + filename, 'width': width, 'height': height,
                            'sha256': digest, 'modified': False, 'verified_at': now.isoformat()}
            state = 'ready'
            counts['saved'] += 1
        except Unavailable as exc:
            state = exc.state
            counts['unavailable'] += 1
        except Exception:
            # Malformed publisher HTML/XML/images must not cancel the edition.
            counts['unavailable'] += 1
        checks[key] = {'state': state, 'checked_at': now.isoformat()}
        write(path, catalog)
    return counts


class BudgetFetcher:
    """Bound network work across discovery, image downloads and both collectors."""
    def __init__(self, fetch, seconds=420, maximum_calls=100):
        self.fetch = fetch
        self.deadline = time.monotonic() + max(0, seconds)
        self.remaining = maximum_calls

    def __call__(self, url, limit=8_000_000):
        if self.remaining <= 0 or time.monotonic() >= self.deadline:
            raise Unavailable('budget_exhausted')
        self.remaining -= 1
        result = self.fetch(url, limit)
        self.last_url = getattr(self.fetch, 'last_url', url)
        return result


def collect_article_figures(root=ROOT, fetch=None, now=None, *, retry=False,
                            time_budget=180, maximum_papers=None, papers=None):
    """Collect multiple originals for all retained readings, independently
    of single card thumbnails. Successful assets survive every later failure.
    """
    from src.article_figures import MAX_ARTICLE_FIGURES, eligible_article_figures
    from src.auto_reading import load
    from src.editions import entries as editions, enrich, relative_path, selected
    from src.figures import figure_key
    from src.paper_identity import paper_doi

    root = Path(root)
    fetch = BudgetFetcher(fetch or Fetcher(), time_budget)
    now = now or datetime.now(timezone.utc)
    path = root / 'data/figures/catalog.json'
    catalog = read(path)
    entries = catalog.setdefault('article_entries', {})
    checks = catalog.setdefault('article_checks', {})
    if papers is None:
        analyses = load(root / 'data')
        sources = [read(root / 'data/daily.json')]
        sources += [read(root / 'data' / relative_path(e)) for e in reversed(editions(root / 'data'))]
        papers = [paper for source in sources for paper in selected(enrich(source, analyses))]
    counts = {'saved': 0, 'existing': 0, 'unavailable': 0}
    seen, attempts = set(), 0
    for paper in papers:
        if (maximum_papers is not None and attempts >= maximum_papers) or time.monotonic() >= fetch.deadline:
            break
        key = figure_key(paper)
        if not key or key in seen:
            continue
        seen.add(key)
        old_check = checks.get(key, {})
        existing = eligible_article_figures(root, paper)
        stored = entries.get(key, [])
        existing_labels = {f['figure_label'] for f in existing}
        intact = bool(stored) and all(f.get('figure_label') in existing_labels for f in stored)
        try:
            age = (now - datetime.fromisoformat(old_check.get('checked_at', ''))).total_seconds()
        except (ValueError, TypeError):
            age = float('inf')
        lifetime = 7 * 86400 if old_check.get('state') in ('ready', 'not_found', 'license_unconfirmed') else 86400
        if not retry and 0 <= age < lifetime and (old_check.get('state') != 'ready' or intact):
            counts['existing' if intact else 'unavailable'] += 1
            continue
        attempts += 1
        state = 'unavailable'
        try:
            doi = paper_doi(paper)
            if doi.startswith('10.1038/'):
                candidates = nature_figures(doi, fetch, maximum=MAX_ARTICLE_FIGURES)
            else:
                try:
                    candidates = publisher_figures(doi, paper, fetch, maximum=MAX_ARTICLE_FIGURES)
                except Unavailable as publisher_error:
                    if not doi:
                        raise
                    try:
                        candidates = pmc_figures(doi, fetch, maximum=MAX_ARTICLE_FIGURES)
                    except Unavailable:
                        raise publisher_error
            saved = []
            for metadata, data in candidates[:MAX_ARTICLE_FIGURES]:
                try:
                    width, height, extension = image_info(data)
                except Exception:
                    continue
                digest = hashlib.sha256(data).hexdigest()
                filename = 'auto-' + hashlib.sha256(key.encode()).hexdigest()[:16] + '-' + digest[:12] + '.' + extension
                destination = path.parent / 'images' / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix('.tmp')
                temporary.write_bytes(data)
                temporary.replace(destination)
                saved.append({**metadata, 'image_path': 'assets/figures/' + filename,
                              'width': width, 'height': height, 'sha256': digest,
                              'modified': False, 'verified_at': now.isoformat()})
            if saved:
                # Partial publisher responses cannot remove already usable panels.
                by_label = {f['figure_label']: f for f in stored if isinstance(f, dict) and f.get('figure_label')}
                by_label.update({f['figure_label']: f for f in saved})
                entries[key] = list(by_label.values())[:MAX_ARTICLE_FIGURES]
                counts['saved'] += len(saved)
                state = 'ready'
            else:
                counts['unavailable'] += 1
        except Unavailable as error:
            state = error.state
            counts['unavailable'] += 1
        except Exception:
            counts['unavailable'] += 1
        checks[key] = {'state': state, 'checked_at': now.isoformat()}
        write(path, catalog)
    return counts


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--retry', action='store_true', help='Retry unavailable figures now; existing images are reused')
    retry = parser.parse_args().retry
    fetch = BudgetFetcher(Fetcher(), seconds=420)
    articles = collect_article_figures(fetch=fetch, retry=retry)
    cards = collect(fetch=fetch, retry=retry, time_budget=max(0, fetch.deadline-time.monotonic()))
    print(json.dumps({'cards': cards, 'articles': articles}, ensure_ascii=False))
