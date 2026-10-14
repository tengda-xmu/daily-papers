"""Offline, byte-verified original figures eligible for editorial group layouts."""
from __future__ import annotations

import hashlib
from io import BytesIO
import json
import re
import shutil
import time
from pathlib import Path

from PIL import Image

from src.figures import figure_key, read_catalog

MAX_ARTICLE_FIGURES = 12
ASSET = re.compile(r'assets/figures/[a-z0-9-]+\.(?:png|jpg|jpeg)')
LICENSE = re.compile(r'https://creativecommons\.org/licenses/by(?:-nc)?(?:-nd|-sa)?/(?:3\.0|4\.0)/')
PUBLIC_FIELDS = ('figure_label', 'title', 'caption', 'title_zh', 'caption_zh', 'credit',
                 'source_url', 'source_image_url', 'license', 'license_url', 'license_source',
                 'width', 'height', 'sha256', 'modified', 'verified_at', 'pdf_page', 'extraction')


def article_figure_file(root, figure):
    """Resolve only collected public originals; never uploaded/private PDFs."""
    asset = figure.get('image_path') or figure.get('asset')
    if not isinstance(asset, str) or not ASSET.fullmatch(asset):
        return None
    root = Path(root).resolve()
    for base in (root / 'tools/assets/figures', root / 'data/figures/images',
                 root / 'data/cache/article-figures/images'):
        candidate = (base / Path(asset).name).resolve()
        if candidate.parent == base.resolve() and candidate.is_file():
            return candidate
    return None


def _public_url(value):
    try:
        from src.reading_article import public_url
        return public_url(value)
    except (TypeError, ValueError):
        return None


def _metadata_valid(figure):
    if not isinstance(figure, dict) or figure.get('modified') is not False:
        return False
    if not LICENSE.fullmatch(str(figure.get('license_url', ''))):
        return False
    if not all(_public_url(figure.get(k)) for k in ('source_url', 'license_source')):
        return False
    if not all(isinstance(figure.get(k), str) and figure[k].strip()
               for k in ('credit', 'license', 'figure_label')):
        return False
    for field in ('figure_label', 'title', 'caption', 'title_zh', 'caption_zh', 'credit', 'license', 'verified_at', 'extraction'):
        if field in figure and (not isinstance(figure[field], str) or len(figure[field]) > 20000 or '\x00' in figure[field]):
            return False
    caption = figure.get('caption') or figure.get('caption_zh')
    if not isinstance(caption, str) or not caption.strip() or len(caption) > 20000:
        return False
    if not all(type(figure.get(k)) is int and figure[k] >= 200 for k in ('width', 'height')):
        return False
    if figure['width'] * figure['height'] > 40_000_000:
        return False
    return bool(ASSET.fullmatch(str(figure.get('image_path') or figure.get('asset') or '')))


def _image_valid(figure, data):
    if len(data) > 20_000_000 or hashlib.sha256(data).hexdigest() != figure.get('sha256'):
        return False
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ('PNG', 'JPEG') or image.size != (figure['width'], figure['height']):
                return False
            image.verify()
    except (OSError, ValueError, Image.DecompressionBombError):
        return False
    return True


def _verified(root, key, figure):
    if not _metadata_valid(figure):
        return None
    path = article_figure_file(root, figure)
    if path is None:
        return None
    try:
        if path.stat().st_size > 20_000_000:
            return None
        if not _image_valid(figure, path.read_bytes()):
            return None
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    label = re.sub(r'[^a-z0-9]', '', figure['figure_label'].casefold()).replace('figure', 'fig')
    identifier = 'fig-' + hashlib.sha256(f'{key}\n{label}\n{figure["sha256"]}'.encode()).hexdigest()[:24]
    result = {k: figure[k] for k in PUBLIC_FIELDS if k in figure}
    result.update(figure_id=identifier, caption=(figure.get('caption') or figure['caption_zh']).strip(),
                  caption_basis=figure.get('caption_basis') if figure.get('caption_basis') in ('publisher', 'curated_translation')
                  else ('publisher' if figure.get('caption') else 'curated_translation'),
                  image_path=figure.get('image_path') or figure['asset'],
                  asset=figure.get('image_path') or figure['asset'])
    if result.get('source_image_url') and not _public_url(result['source_image_url']):
        result.pop('source_image_url')
    if type(result.get('pdf_page')) is not int or result.get('pdf_page', 0) < 1:
        result.pop('pdf_page', None)
    return result


def eligible_article_figures(root, paper, material=None):
    """Return stable public descriptors; this function never fetches or mutates.

    Material is accepted for worker compatibility. Public originals always need
    publisher-confirmed licensing; local material cannot grant reuse permission.
    """
    root = Path(root)
    key = figure_key(paper)
    if not key:
        return []
    automatic = read_catalog(root / 'data/figures/catalog.json')
    curated = read_catalog(root / 'data/curated/figures.json')
    synchronized = read_catalog(root / 'data/cache/article-figures/catalog.json')
    candidates = []
    # A successfully synchronized complete list (including an explicit empty
    # list) supersedes stale automatic metadata. Curated local entries remain
    # an explicit user-owned override.
    catalogs = (curated, synchronized) if key in synchronized.get('authoritative_keys', []) else (curated, synchronized, automatic)
    for catalog in catalogs:
        entries = catalog.get('entries', {})
        if isinstance(entries, dict) and isinstance(entries.get(key), dict):
            candidates.append(entries[key])
        article_entries = catalog.get('article_entries', {})
        rows = article_entries.get(key, []) if isinstance(article_entries, dict) else []
        if isinstance(rows, list):
            candidates.extend(rows[:MAX_ARTICLE_FIGURES])
    result, labels = [], set()
    for candidate in candidates:
        figure = _verified(root, key, candidate)
        if figure is None:
            continue
        label = re.sub(r'[^a-z0-9]', '', figure['figure_label'].casefold()).replace('figure', 'fig')
        if label in labels:
            continue
        labels.add(label)
        result.append(figure)
        if len(result) == MAX_ARTICLE_FIGURES:
            break
    return result


def copy_article_figures(root, paper, out, material=None):
    """Copy only validated originals into a static build, returning descriptors."""
    figures = eligible_article_figures(root, paper, material)
    out = Path(out).resolve()
    for figure in figures:
        source = article_figure_file(root, figure)
        destination = out / figure['asset']
        destination.parent.mkdir(parents=True, exist_ok=True)
        # A viewer may memory-map an existing original on Windows. Do not
        # truncate an already byte-verified asset merely to copy it again.
        if (destination.is_file() and destination.stat().st_size <= 20_000_000
                and hashlib.sha256(destination.read_bytes()).hexdigest() == figure['sha256']):
            continue
        if source != destination.resolve():
            shutil.copyfile(source, destination)
    return figures


def public_article_figure_manifest(root, papers):
    """Publish already-verified catalog descriptors for the local reading worker."""
    result = {'version': 1, 'papers': {}, 'paper_keys': {}}
    for paper in papers:
        identifier = paper.get('id', '')
        if not re.fullmatch(r'[a-f0-9]{12}', str(identifier)):
            continue
        key = figure_key(paper)
        if key:
            result['papers'][identifier] = eligible_article_figures(root, paper)
            result['paper_keys'][identifier] = key
    return result


def sync_public_article_figures(root, papers, fetch_json, fetch_image=None, *,
                               maximum_images=12, time_budget=40):
    """Synchronize bounded public originals; failures never remove good records."""
    import requests
    from tools.recommendation_data import PUBLIC

    root = Path(root)
    counts = {'saved': 0, 'existing': 0, 'unavailable': 0}
    deadline = time.monotonic() + time_budget
    try:
        manifest = fetch_json('article-figures.json')
        if (not isinstance(manifest, dict) or manifest.get('version') != 1
                or not isinstance(manifest.get('papers'), dict)
                or not isinstance(manifest.get('paper_keys'), dict)):
            raise ValueError('Invalid public figure manifest')
    except Exception:
        counts['unavailable'] += 1
        return counts

    remaining_bytes = 50_000_000

    def download(asset):
        with requests.get(PUBLIC + asset, timeout=(5, 10), stream=True, allow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError('Public figure unavailable')
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > min(20_000_000, remaining_bytes) or time.monotonic() >= deadline:
                    raise ValueError('Public figure download budget exhausted')
                chunks.append(chunk)
            return b''.join(chunks)

    cache = root / 'data/cache/article-figures'
    catalog = read_catalog(cache / 'catalog.json')
    entries = catalog.get('article_entries', {})
    if not isinstance(entries, dict):
        entries = {}
    catalog['article_entries'] = entries
    authoritative = set(catalog.get('authoritative_keys', []))
    attempted = 0
    for paper in papers:
        identifier, key = paper.get('id'), figure_key(paper)
        if not key or manifest['paper_keys'].get(identifier) != key or identifier not in manifest['papers']:
            continue
        candidates = manifest['papers'][identifier]
        if (not isinstance(candidates, list) or len(candidates) > MAX_ARTICLE_FIGURES
                or any(not _metadata_valid(row) for row in candidates)):
            counts['unavailable'] += 1
            continue
        saved, labels, complete = [], set(), True
        for row in candidates:
            label = re.sub(r'[^a-z0-9]', '', row['figure_label'].casefold()).replace('figure', 'fig')
            expected = 'fig-' + hashlib.sha256(f'{key}\n{label}\n{row.get("sha256")}'.encode()).hexdigest()[:24]
            if row.get('figure_id') != expected or label in labels:
                counts['unavailable'] += 1
                complete = False
                break
            labels.add(label)
            verified = _verified(root, key, row)
            if verified:
                saved.append(verified)
                counts['existing'] += 1
                continue
            # Keep examining later papers after exhausting image downloads:
            # their complete cached list or explicit removal needs no network.
            if time.monotonic() >= deadline or attempted >= maximum_images:
                complete = False
                break
            attempted += 1
            asset = row.get('image_path') or row['asset']
            try:
                data = (fetch_image or download)(asset)
                if not isinstance(data, bytes) or not _image_valid(row, data):
                    raise ValueError('Public figure integrity mismatch')
                remaining_bytes -= len(data)
                if remaining_bytes < 0:
                    raise ValueError('Public figure byte budget exhausted')
                destination = cache / 'images' / Path(asset).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                temporary = destination.with_suffix('.tmp')
                temporary.write_bytes(data)
                temporary.replace(destination)
                verified = _verified(root, key, row)
                if verified is None:
                    raise ValueError('Public figure verification failed')
                saved.append(verified)
                counts['saved'] += 1
            except Exception:
                counts['unavailable'] += 1
                complete = False
        if complete:
            # Atomic per-paper replacement: removal is authoritative only after
            # the entire advertised set is validated. Interrupted downloads
            # retain the previous set, with verified bytes reusable next sync.
            entries[key] = saved
            authoritative.add(key)
    if entries:
        catalog['authoritative_keys'] = sorted(authoritative)
        cache.mkdir(parents=True, exist_ok=True)
        temporary = cache / 'catalog.tmp'
        temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        temporary.replace(cache / 'catalog.json')
    return counts
