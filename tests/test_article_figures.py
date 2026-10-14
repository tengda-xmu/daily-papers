from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from zipfile import ZipFile

import fitz
import pytest

from src.article_figures import (article_figure_file, copy_article_figures, eligible_article_figures,
                                 public_article_figure_manifest, sync_public_article_figures)
from tests.test_figure_collection import DOI, META, nature_html, png
from tools.collect_figures import (BudgetFetcher, Unavailable, collect_article_figures,
                                   nature_figures, pdf_figures, pmc_figures, publisher_figures)

PAPER = {'id': '123456789abc', 'doi': DOI, 'analysis_basis': 'full_text'}
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def original(root, number, *, curated=False):
    asset = f'assets/figures/original-{number}.png'
    path = root / ('tools/' + asset if curated else f'data/figures/images/original-{number}.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png())
    return {**META, 'figure_label': f'Fig. {number}', 'image_path': asset,
            'caption': f'Original caption {number}.', 'width': 600, 'height': 400,
            'sha256': hashlib.sha256(png()).hexdigest(), 'modified': False}


def test_legacy_catalog_and_new_multiple_figures_preserve_attribution_and_identity(tmp_path):
    one = original(tmp_path, 1, curated=True)
    two = original(tmp_path, 2)
    write(tmp_path / 'data/curated/figures.json', {'entries': {DOI: one}})
    write(tmp_path / 'data/figures/catalog.json', {'entries': {DOI: two}, 'article_entries': {DOI: [one, two]}})
    figures = eligible_article_figures(tmp_path, PAPER)
    assert [f['figure_label'] for f in figures] == ['Fig. 1', 'Fig. 2']
    assert len({f['figure_id'] for f in figures}) == 2
    assert figures == eligible_article_figures(tmp_path, {**PAPER, 'doi': 'https://doi.org/' + DOI})
    out = tmp_path / 'build'
    assert copy_article_figures(tmp_path, PAPER, out) == figures
    for figure in figures:
        assert (out / figure['asset']).read_bytes() == png()
        assert figure['license_url'] == META['license_url']
        assert figure['caption_basis'] == 'publisher'
    assert eligible_article_figures(tmp_path, {'doi': '10.1038/other'}) == []


@pytest.mark.parametrize('field,value', [
    ('image_path', 'assets/figures/../../private.png'), ('license_url', 'https://example.org/license'),
    ('source_url', 'javascript:alert(1)'), ('credit', ''), ('modified', True),
    ('sha256', '0' * 64), ('width', 601), ('caption', {'untrusted': 'object'}),
])
def test_unverified_or_changed_figures_are_not_available_to_writer_or_renderer(tmp_path, field, value):
    figure = {**original(tmp_path, 1), field: value}
    write(tmp_path / 'data/figures/catalog.json', {'article_entries': {DOI: [figure]}})
    assert eligible_article_figures(tmp_path, PAPER) == []


def test_private_material_never_grants_public_figure_rights(tmp_path):
    figure = original(tmp_path, 1)
    figure.pop('license_url')
    write(tmp_path / 'data/figures/catalog.json', {'article_entries': {DOI: [figure]}})
    assert eligible_article_figures(tmp_path, PAPER, {'figures': [figure], 'basis': 'full_text'}) == []


def test_rebuild_preserves_verified_open_original_but_repairs_corruption(tmp_path, monkeypatch):
    import shutil
    figure = original(tmp_path, 1)
    write(tmp_path / 'data/figures/catalog.json', {'article_entries': {DOI: [figure]}})
    out = tmp_path / 'site'
    copy_article_figures(tmp_path, PAPER, out)
    real_copy = shutil.copyfile
    def mapped_file(*_):
        raise OSError(22, 'Original is memory mapped by a viewer')
    monkeypatch.setattr(shutil, 'copyfile', mapped_file)
    copy_article_figures(tmp_path, PAPER, out)
    destination = out / figure['image_path']
    assert destination.read_bytes() == png()
    destination.write_bytes(b'corrupted image')
    monkeypatch.setattr(shutil, 'copyfile', real_copy)
    copy_article_figures(tmp_path, PAPER, out)
    assert destination.read_bytes() == png()


def test_multiple_publisher_originals_keep_labels_and_exclude_third_party_images():
    html = nature_html() + '''<figure><figcaption>Fig. 3: Proposed workflow</figcaption>
    <img src="https://media.springernature.com/lw685/image/10.1038/example/Fig3.png">
    <div class="c-article-section__figure-description">The original method.</div></figure>'''
    calls = []
    def fetch(url, limit=0):
        calls.append(url)
        return png() if url.endswith('.png') else html.encode()
    figures = nature_figures(DOI, fetch)
    assert [f[0]['figure_label'] for f in figures] == ['Fig. 3', 'Fig. 2']
    assert len(calls) == 3
    assert all(f[1] == png() for f in figures)


def test_multiple_pdf_figures_keep_all_vector_panels_and_original_page_labels():
    document = fitz.open()
    for index, caption in enumerate(('Fig. 1: Method overview.', 'Fig. 2: Experimental results.')):
        page = document.new_page()
        page.insert_text((30, 30), DOI)
        page.draw_rect(fitz.Rect(80, 100, 240, 260), color=(0, 0, 1))
        page.draw_rect(fitz.Rect(280, 100, 440, 260), color=(1, 0, 0))
        page.insert_text((85, 95), '(a) Stress')
        page.insert_text((285, 95), '(b) Fatigue life')
        page.insert_text((80, 290), caption)
    figures = pdf_figures(DOI, META['license_source'], META, document.tobytes())
    document.close()
    assert [f[0]['pdf_page'] for f in figures] == [1, 2]
    assert all(f[0]['region'][0] <= 80 and f[0]['region'][2] >= 440 for f in figures)
    assert all(f[0]['region'][1] < 95 for f in figures)


def test_publisher_multi_collection_bounds_figures_and_keeps_original_source(monkeypatch):
    url = 'https://www.sciencedirect.com/science/article/pii/TEST'
    html = (f'<meta name="citation_doi" content="{DOI}"><div class="copyright">'
            f'<a href="{META["license_url"]}">License</a></div>')
    for index in range(1, 5):
        html += (f'<figure id="fig{index}"><figcaption>Fig. {index}: Results</figcaption>'
                 f'<img src="https://ars.els-cdn.com/figure{index}.png"></figure>')
    monkeypatch.setattr('tools.collect_figures.discover', lambda *args: {'urls': [url]})
    calls = []
    def fetch(target, limit=0):
        calls.append(target)
        return png() if target.endswith('.png') else html.encode()
    figures = publisher_figures(DOI, {**PAPER, 'authors': ['A. Author']}, fetch, maximum=2)
    assert len(figures) == 2 and len(calls) == 3
    assert [f[0]['source_url'] for f in figures] == [url + '#fig1', url + '#fig2']


def test_pmc_multiple_figures_fetch_bundle_once_and_skip_missing_archive_entry():
    document = (f'<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>'
                f'<article-id pub-id-type="doi">{DOI}</article-id><contrib-group><contrib>'
                f'<name>A. Author</name></contrib></contrib-group><permissions>'
                f'<license xlink:href="{META["license_url"]}" /></permissions></article-meta></front><body>')
    for index in range(1, 4):
        document += (f'<fig id="F{index}"><label>Fig. {index}</label><caption><title>Results</title>'
                     f'<p>Original caption.</p></caption><graphic xlink:href="figure{index}.png" /></fig>')
    document += '</body></article>'
    archive = BytesIO()
    with ZipFile(archive, 'w') as bundle:
        bundle.writestr('figure1.png', png())
        bundle.writestr('figure3.png', png())
    calls = []
    def fetch(target, limit=0):
        calls.append(target)
        if 'search?' in target:
            return json.dumps({'resultList': {'result': [{'doi': DOI, 'pmcid': 'PMC123'}]}}).encode()
        return document.encode() if target.endswith('fullTextXML') else archive.getvalue()
    figures = pmc_figures(DOI, fetch)
    assert [f[0]['figure_label'] for f in figures] == ['Fig. 1', 'Fig. 3']
    assert len([url for url in calls if url.endswith('supplementaryFiles')]) == 1


def test_article_collection_prioritizes_current_fulltext_and_preserves_after_failure(tmp_path, monkeypatch):
    write(tmp_path / 'data/daily.json', {'core': [PAPER], 'extended': [
        {'id': '222222222222', 'doi': '10.1038/abstract', 'analysis_basis': 'abstract'},
        {'id': '333333333333', 'doi': '10.1038/extended', 'analysis_basis': 'full_text'}]})
    calls = []
    def collect(doi, fetch, maximum):
        calls.append(doi)
        return [({**META, 'figure_label': 'Fig. 1'}, png()), ({**META, 'figure_label': 'Fig. 2'}, png())]
    monkeypatch.setattr('tools.collect_figures.nature_figures', collect)
    assert collect_article_figures(tmp_path, now=NOW, maximum_papers=1)['saved'] == 2
    assert calls == [DOI]
    assert len(eligible_article_figures(tmp_path, PAPER)) == 2
    assert collect_article_figures(tmp_path, now=NOW)['saved'] == 2
    assert calls == [DOI, '10.1038/extended']
    assert collect_article_figures(tmp_path, now=NOW + timedelta(days=6))['existing'] == 2
    def fail(*args, **kwargs):
        raise Unavailable('restricted')
    monkeypatch.setattr('tools.collect_figures.nature_figures', fail)
    assert collect_article_figures(tmp_path, now=NOW + timedelta(days=7))['unavailable'] == 2
    assert len(eligible_article_figures(tmp_path, PAPER)) == 2
    before = (tmp_path / 'data/figures/catalog.json').read_bytes()
    assert collect_article_figures(tmp_path, now=NOW + timedelta(days=7, hours=2))['saved'] == 0
    assert (tmp_path / 'data/figures/catalog.json').read_bytes() == before


def test_budget_fetcher_does_not_start_extra_network_work():
    calls = []
    fetch = BudgetFetcher(lambda url, limit: calls.append(url) or b'ok', maximum_calls=1)
    assert fetch('https://www.nature.com/first') == b'ok'
    with pytest.raises(Unavailable) as exc:
        fetch('https://www.nature.com/second')
    assert exc.value.state == 'budget_exhausted' and len(calls) == 1


def test_published_figures_sync_to_local_worker_and_match_build_ids(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    one, two = original(publisher, 1), original(publisher, 2)
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [one, two]}})
    manifest = public_article_figure_manifest(publisher, [PAPER])
    calls = []
    def download(asset):
        calls.append(asset)
        return article_figure_file(publisher, {'asset': asset}).read_bytes()
    result = sync_public_article_figures(local, [PAPER], lambda path: manifest, download, maximum_images=1)
    assert result['saved'] == 1 and len(calls) == 1
    # Publish the new set atomically only when every advertised original has
    # passed validation; the first downloaded file is reusable next sync.
    assert len(eligible_article_figures(local, PAPER)) == 0
    result = sync_public_article_figures(local, [PAPER], lambda path: manifest, download)
    assert result == {'saved': 1, 'existing': 1, 'unavailable': 0}
    assert eligible_article_figures(local, PAPER) == eligible_article_figures(publisher, PAPER)
    assert not (local / 'data/figures/catalog.json').exists()
    assert all('cache/article-figures/images' in article_figure_file(local, f).as_posix()
               for f in eligible_article_figures(local, PAPER))


def test_public_sync_rejects_wrong_paper_corrupt_bytes_and_unsafe_paths(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    one = original(publisher, 1)
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [one]}})
    manifest = public_article_figure_manifest(publisher, [PAPER])
    calls = []
    wrong = deepcopy(manifest)
    wrong['paper_keys'][PAPER['id']] = '10.1038/other'
    sync_public_article_figures(local, [PAPER], lambda path: wrong, lambda asset: calls.append(asset))
    assert calls == []
    result = sync_public_article_figures(local, [PAPER], lambda path: manifest, lambda asset: b'bad')
    assert result['unavailable'] == 1 and not (local / 'data/cache').exists()
    wrong = deepcopy(manifest)
    wrong['papers'][PAPER['id']][0]['image_path'] = '../private.png'
    result = sync_public_article_figures(local, [PAPER], lambda path: wrong, lambda asset: calls.append(asset))
    assert result['unavailable'] == 1 and calls == []
    sync_public_article_figures(local, [PAPER], lambda path: manifest, lambda asset: png())
    before = eligible_article_figures(local, PAPER)
    def failure(path):
        raise OSError('offline')
    assert sync_public_article_figures(local, [PAPER], failure)['unavailable'] == 1
    assert eligible_article_figures(local, PAPER) == before


def test_cloud_corrections_override_stale_automatic_but_keep_local_curated(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    latest = {**original(publisher, 1), 'caption': 'Corrected publisher caption.'}
    old = {**original(local, 1), 'caption': 'Stale automatic caption.'}
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [latest]}})
    write(local / 'data/figures/catalog.json', {'article_entries': {DOI: [old]}})
    manifest = public_article_figure_manifest(publisher, [PAPER])
    sync_public_article_figures(local, [PAPER], lambda path: manifest, lambda asset: png())
    assert eligible_article_figures(local, PAPER)[0]['caption'] == latest['caption']
    curated = {**original(local, 1, curated=True), 'caption': 'Explicit local curation.'}
    write(local / 'data/curated/figures.json', {'entries': {DOI: curated}})
    assert eligible_article_figures(local, PAPER)[0]['caption'] == curated['caption']


def test_explicit_removal_prunes_synced_set_and_suppresses_stale_auto_fallback(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    one, two = original(publisher, 1), original(publisher, 2)
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [one, two]}})
    old = original(local, 1)
    write(local / 'data/figures/catalog.json', {'article_entries': {DOI: [old]}})
    manifest = public_article_figure_manifest(publisher, [PAPER])
    sync_public_article_figures(local, [PAPER], lambda path: manifest, lambda asset: png())
    assert len(eligible_article_figures(local, PAPER)) == 2
    manifest['papers'][PAPER['id']] = manifest['papers'][PAPER['id']][1:]
    sync_public_article_figures(local, [PAPER], lambda path: manifest, maximum_images=0)
    assert [f['figure_label'] for f in eligible_article_figures(local, PAPER)] == ['Fig. 2']
    write(publisher / 'data/figures/catalog.json', {})
    empty = public_article_figure_manifest(publisher, [PAPER])
    assert empty['paper_keys'][PAPER['id']] == DOI and empty['papers'][PAPER['id']] == []
    sync_public_article_figures(local, [PAPER], lambda path: empty, maximum_images=0)
    assert eligible_article_figures(local, PAPER) == []
    assert (local / 'data/figures/images/original-1.png').is_file()


def test_incomplete_manifest_or_failed_replacement_preserves_old_complete_set(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    one, two = original(publisher, 1), original(publisher, 2)
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [one]}})
    manifest = public_article_figure_manifest(publisher, [PAPER])
    sync_public_article_figures(local, [PAPER], lambda path: manifest, lambda asset: png())
    before = eligible_article_figures(local, PAPER)
    missing = deepcopy(manifest)
    missing['papers'].pop(PAPER['id'])
    sync_public_article_figures(local, [PAPER], lambda path: missing)
    assert eligible_article_figures(local, PAPER) == before
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [two]}})
    replacement = public_article_figure_manifest(publisher, [PAPER])
    sync_public_article_figures(local, [PAPER], lambda path: replacement, lambda asset: b'bad')
    assert eligible_article_figures(local, PAPER) == before
    malformed = deepcopy(replacement)
    malformed['papers'][PAPER['id']][0]['license_url'] = 'https://example.org/unconfirmed'
    sync_public_article_figures(local, [PAPER], lambda path: malformed)
    assert eligible_article_figures(local, PAPER) == before
    forged = deepcopy(manifest)
    forged['papers'][PAPER['id']][0]['figure_id'] = 'fig-forged'
    sync_public_article_figures(local, [PAPER], lambda path: forged)
    assert eligible_article_figures(local, PAPER) == before


def test_later_paper_removal_is_processed_after_download_budget_is_exhausted(tmp_path):
    publisher, local = tmp_path / 'publisher', tmp_path / 'local'
    next_paper = {**PAPER, 'id': '222222222222', 'doi': '10.1038/next'}
    next_original = original(local, 3)
    write(local / 'data/figures/catalog.json', {'article_entries': {next_paper['doi']: [next_original]}})
    one = original(publisher, 1)
    write(publisher / 'data/figures/catalog.json', {'article_entries': {DOI: [one]}})
    manifest = public_article_figure_manifest(publisher, [PAPER, next_paper])
    assert len(eligible_article_figures(local, next_paper)) == 1
    result = sync_public_article_figures(local, [PAPER, next_paper], lambda path: manifest,
                                        lambda asset: png(), maximum_images=1)
    assert result['saved'] == 1 and len(eligible_article_figures(local, PAPER)) == 1
    assert eligible_article_figures(local, next_paper) == []
