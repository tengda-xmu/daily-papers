from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from zipfile import ZipFile

import fitz
from PIL import Image
import pytest

from tools.collect_figures import (Unavailable, collect, image_info, license_info,
                                   nature_figure, pdf_figure, pmc_figure, Fetcher)

DOI = '10.1038/example'
LICENSE = 'https://creativecommons.org/licenses/by/4.0/'
META = {'figure_label': 'Fig. 2', 'title': 'Experiment', 'caption': 'Results.',
        'credit': 'A. Author', 'license': 'CC BY 4.0', 'license_url': LICENSE,
        'license_source': 'https://www.nature.com/articles/example',
        'source_url': 'https://www.nature.com/articles/example/figures/2'}


def png():
    output = BytesIO()
    Image.new('RGB', (600, 400), 'white').save(output, 'PNG')
    return output.getvalue()


def nature_html(doi=DOI, rights=True):
    def figure(number, image_doi=DOI, text='Results.'):
        return f'''<figure><figcaption>Fig. {number}: Experiment</figcaption>
        <img src="https://media.springernature.com/lw685/image/{image_doi}/Fig{number}.png">
        <div class="c-article-section__figure-description">{text}</div></figure>'''
    return (f'<meta name="citation_doi" content="{doi}"><meta name="citation_author" content="A. Author">'
            + (f'<section><h2 id="rightslink">Rights</h2><a href="{LICENSE}">License</a></section>' if rights else '')
            + figure(1, text='Created in BioRender.') + figure(7, image_doi='10.1038/other') + figure(2))


def test_nature_checks_identity_and_skips_third_party_or_related_images():
    calls = []
    image = png()
    def fetch(url, limit=0):
        calls.append(url)
        return nature_html().encode() if len(calls) == 1 else image
    info, data = nature_figure(DOI, fetch)
    assert data == image
    assert info['figure_label'] == 'Fig. 2'
    assert info['title'] == 'Experiment'
    assert '/full/' in calls[-1] and calls[-1].endswith('/Fig2.png')
    for html, state in ((nature_html('10.1038/wrong'), 'unavailable'), (nature_html(rights=False), 'license_unconfirmed')):
        with pytest.raises(Unavailable) as exc:
            nature_figure(DOI, lambda url: html.encode())
        assert exc.value.state == state


def test_licenses_are_exact_and_requests_cannot_follow_arbitrary_hosts():
    assert license_info(['http://creativecommons.org/licenses/by-nc-nd/4.0/'])['license'] == 'CC BY-NC-ND 4.0'
    for url in ('https://creativecommons.org.evil/licenses/by/4.0/', 'https://example.com/free'):
        with pytest.raises(Unavailable):
            license_info([url])
    for url in ('http://www.nature.com/a', 'https://127.0.0.1/a', 'https://www.nature.com:444/a'):
        with pytest.raises(Unavailable):
            Fetcher()(url)


def pdf(ambiguous=False):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((30, 30), DOI)
    page.insert_image(fitz.Rect(30, 70, 430, 337), stream=png())
    page.insert_text((30, 365), 'Fig. 1 | Experimental results.')
    if ambiguous:
        page.insert_text((30, 400), 'Fig. 2 | Another figure.')
    result = doc.tobytes()
    doc.close()
    return result


def test_pdf_extracts_complete_embedded_image_and_rejects_ambiguity():
    info, data = pdf_figure(DOI, META['license_source'] + '.pdf', META, pdf())
    assert info['pdf_page'] == 1 and info['figure_label'] == 'Fig. 1'
    assert info['source_url'].endswith('#page=1')
    assert image_info(data)[:2] == (600, 400)
    for doi, document in (('10.1038/other', pdf()), (DOI, pdf(True))):
        with pytest.raises(Unavailable):
            pdf_figure(doi, META['license_source'], META, document)


def test_pmc_verifies_doi_license_and_reads_only_the_exact_figure():
    doi = '10.1126/example'
    document = f'''<article xmlns:xlink="http://www.w3.org/1999/xlink"><front><article-meta>
    <article-id pub-id-type="doi">{doi}</article-id><contrib-group><contrib><name>A. Author</name></contrib></contrib-group>
    <permissions><license xlink:href="{LICENSE}" /></permissions></article-meta></front><body><fig id="F1">
    <label>Fig. 1.</label><caption><title>A diagram</title><p>Results.</p></caption>
    <graphic xlink:href="figure.png" /></fig></body></article>'''
    archive = BytesIO()
    with ZipFile(archive, 'w') as bundle:
        bundle.writestr('figure.png', png())
        bundle.writestr('../unrelated.txt', 'Do not extract')
    def fetch(url, limit=0):
        if 'search?' in url:
            return json.dumps({'resultList': {'result': [{'doi': doi, 'pmcid': 'PMC123'}]}}).encode()
        return document.encode() if url.endswith('fullTextXML') else archive.getvalue()
    info, data = pmc_figure(doi, fetch)
    assert data == png() and info['archive_entry'] == 'figure.png'
    assert info['license_url'] == LICENSE
    document = document.replace(doi, '10.1126/other')
    with pytest.raises(Unavailable):
        pmc_figure(doi, fetch)


def test_collection_is_durable_idempotent_and_failure_does_not_block_other_papers(tmp_path, monkeypatch):
    path = tmp_path / 'data/daily.json'
    path.parent.mkdir()
    original = json.dumps({'core': [{'doi': DOI}, {'doi': '10.1038/failure'}]})
    path.write_text(original)
    calls = []
    def collect_one(doi, fetch):
        calls.append(doi)
        if doi.endswith('failure'):
            raise Unavailable('restricted')
        return META, png()
    monkeypatch.setattr('tools.collect_figures.nature_figure', collect_one)
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    assert collect(tmp_path, fetch=object(), now=now) == {'saved': 1, 'existing': 0, 'unavailable': 1}
    assert path.read_text() == original
    assert collect(tmp_path, fetch=object(), now=now)['saved'] == 0
    assert len(calls) == 2
    assert collect(tmp_path, fetch=object(), now=now + timedelta(days=1))['existing'] == 1
    assert calls == [DOI, '10.1038/failure', '10.1038/failure']
    assert next((tmp_path / 'data/figures/images').iterdir()).read_bytes() == png()


def test_malformed_image_is_never_saved(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/daily.json').write_text(json.dumps({'core': [{'doi': DOI}]}))
    monkeypatch.setattr('tools.collect_figures.nature_figure', lambda doi, fetch: (META, b'<html>blocked</html>'))
    assert collect(tmp_path, fetch=object())['saved'] == 0
    record = json.loads((tmp_path / 'data/figures/catalog.json').read_text())
    assert not record['entries'] and record['checks'][DOI]['state'] == 'unavailable'
