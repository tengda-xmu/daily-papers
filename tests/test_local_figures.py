import json
from pathlib import Path

from connectors.codex_bridge.local_figures import LocalFigures
from connectors.codex_bridge.documents import parse_pdf
from tests.test_codex_bridge import bridge, login, P1
from tests.test_figure_collection import pdf, DOI


def configure(app):
    payload = {'core': [{'id': P1, 'title': 'A verified fatigue prediction methodology', 'doi': DOI, 'authors': ['A. Author']}]}
    (app.state.store.root / 'data/daily.json').write_text(json.dumps(payload), encoding='utf-8')


def test_upload_extracts_original_region_and_exposes_only_paired_endpoints(bridge):
    client, app, _ = bridge
    configure(app)
    headers = login(client, app)
    base = f'/api/papers/{P1}'
    content = pdf()
    assert client.post(base + '/pdf', headers=headers, files={'file': ('publisher.pdf', content, 'application/pdf')}).status_code == 200
    assert list((app.state.store.runtime / 'original-figures').glob('*.png'))
    assert client.get(base + '/original-figure').status_code == 401
    assert client.get(base + '/original-figure/image').status_code == 401
    result = client.get(base + '/original-figure', headers=headers)
    assert result.headers['Cache-Control'] == 'no-store'
    info = result.json()
    assert info['state'] == 'ready' and info['visibility'] == 'local'
    assert info['figure_label'] == 'Fig. 1' and info['pdf_page'] == 1
    assert not any(k in info for k in ('file', 'pages', 'region', 'identity'))
    image = client.get(base + '/original-figure/image?version=' + info['source_hash'], headers=headers)
    assert image.status_code == 200 and image.content.startswith(b'\x89PNG')
    assert image.headers['Cache-Control'] == 'no-store'
    assert client.get(base + '/pdf?version=' + info['source_hash'], headers=headers).content == content
    assert not (app.state.store.root / 'data/figures').exists()
    assert client.get(base + '/original-figure/image?version=' + 'f'*16, headers=headers).status_code == 409
    assert client.get(base + '/original-figure', headers={**headers, 'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.delete(f'/api/library/papers/{P1}', headers=headers).status_code == 200
    assert not list((app.state.store.runtime / 'original-figures').glob('*'))


def test_legacy_upload_is_backfilled_and_cache_survives_restart(bridge, monkeypatch):
    _, app, _ = bridge
    configure(app)
    store = app.state.store
    doc = parse_pdf(pdf(), store.directory(P1))
    store.set_document(P1, doc)
    original_db = store.db.read_bytes()
    first = app.state.local_figures.ensure(P1)
    assert first['state'] == 'ready'
    def unexpected(*args):
        raise AssertionError('The cached original must not be extracted again')
    monkeypatch.setattr('connectors.codex_bridge.local_figures.pdf_figure', unexpected)
    assert LocalFigures(store).ensure(P1) == first
    assert store.db.read_bytes() == original_db


def test_mismatched_pdf_never_supplies_another_papers_figure(bridge):
    client, app, _ = bridge
    configure(app)
    payload = {'core': [{'id': P1, 'title': 'A different paper with a different identity', 'doi': '10.1038/wrong'}]}
    (app.state.store.root / 'data/daily.json').write_text(json.dumps(payload), encoding='utf-8')
    headers = login(client, app)
    assert client.post(f'/api/papers/{P1}/pdf', headers=headers, files={'file': ('wrong.pdf', pdf(), 'application/pdf')}).status_code == 200
    assert client.get(f'/api/papers/{P1}/original-figure', headers=headers).json()['state'] == 'identity_unconfirmed'
    assert not list((app.state.store.runtime / 'original-figures').glob('*.png'))


def test_replacing_pdf_invalidates_old_figure_and_keeps_upload_successful(bridge):
    client, app, _ = bridge
    configure(app)
    headers = login(client, app)
    base = f'/api/papers/{P1}'
    assert client.post(base + '/pdf', headers=headers, files={'file': ('paper.pdf', pdf(), 'application/pdf')}).status_code == 200
    old = client.get(base + '/original-figure', headers=headers).json()
    assert client.post(base + '/pdf', headers=headers, files={'file': ('ambiguous.pdf', pdf(ambiguous=True), 'application/pdf')}).status_code == 200
    assert client.get(base + '/original-figure', headers=headers).json()['state'] == 'not_found'
    assert client.get(base + '/original-figure/image?version=' + old['source_hash'], headers=headers).status_code == 409
