import hashlib
from io import BytesIO
import json
import zipfile

from connectors.codex_bridge.store import Store
from connectors.codex_bridge.documents import parse_pdf
from tests.test_codex_bridge import bridge, login, ask, P1, P2
from tests.test_pdf_translation import paper_pdf, translated_turn
from tests.test_pdf_annotations import mark


def translated(bridge):
    c, app, rpc = bridge
    headers = login(c, app)
    store = app.state.store
    original = parse_pdf(paper_pdf(), store.directory(P1))
    store.set_document(P1, original)
    rpc.turn = translated_turn(rpc)
    response = ask(c, headers, mode='translate', translation_source='layout-bilingual')
    assert '"status": "completed"' in response.text
    return c, app, rpc, headers, original


def test_ratings_conflict_snapshot_sort_filter_and_reload(bridge):
    c, app, _ = bridge
    h = login(c, app)
    endpoint = f'/api/library/papers/{P1}/rating'
    assert c.post(endpoint, json={'rating': 4, 'revision': 0}).status_code == 401
    assert c.post(endpoint, headers=h, json={'rating': 6, 'revision': 0}).status_code == 422
    assert c.post(endpoint, headers=h, json={'rating': True, 'revision': 0}).status_code == 422
    assert c.post(endpoint, headers=h, json={'rating': 4, 'revision': 0}).json() == {'rating': 4, 'revision': 1}
    assert c.post(endpoint, headers=h, json={'rating': 1, 'revision': 0}).status_code == 409
    assert c.post(f'/api/library/papers/{P2}/rating', headers=h, json={'rating': 2, 'revision': 0}).status_code == 200
    data = c.get('/api/library?sort=importance&page_size=1', headers=h).json()
    assert data['total'] == 2 and data['papers'][0]['id'] == P1
    assert c.get('/api/library?min_rating=4', headers=h).json()['total'] == 1
    assert c.get('/api/library?q=Paper%20B', headers=h).json()['papers'][0]['id'] == P2
    # The library is independent of the latest issue and even archive availability.
    (app.state.store.root / 'data/daily.json').unlink()
    reopened = Store(app.state.store.runtime, app.state.store.root)
    assert reopened.paper(P1)['title'] == 'Paper A'
    assert reopened.library.rating(P1)['rating'] == 4
    assert c.get(f'/api/papers/{P1}', headers=h).status_code == 200
    assert c.post(endpoint, headers=h, json={'rating': 0, 'revision': 1}).status_code == 200
    assert c.get('/api/library', headers=h).json()['total'] == 1


def test_pdf_and_reading_survive_clear_replacement_and_restart(bridge):
    c, app, rpc, h, original = translated(bridge)
    base = f'/api/papers/{P1}'
    versions = c.get(base, headers=h).json()['pdf_versions']
    v = next(d for d in versions if d['view'] == 'bilingual')
    content = c.get(base + '/pdf?version=' + v['hash'], headers=h).content
    assert c.post(base + '/annotations', headers=h, json={'document_hash': v['hash'], 'revision': 0, 'items': [mark()]}).status_code == 200
    pos = {'version': v['hash'], 'page': 3, 'rx': .2, 'ry': .4, 'zoom': '1.5'}
    assert c.post(f'/api/library/papers/{P1}/reading', headers=h, json=pos).status_code == 200
    assert c.post(f'/api/library/papers/{P1}/rating', headers=h, json={'rating': 5, 'revision': 0}).status_code == 200
    assert c.delete(base, headers=h).status_code == 200
    data = c.get(base, headers=h).json()
    assert data['history'] == [] and len(data['pdf_versions']) == 3
    assert data['reading']['last_version'] == v['hash']
    assert data['reading']['positions'][v['hash']]['page'] == 3
    assert c.get(base + '/annotations?version=' + v['hash'], headers=h).json()['items']
    assert c.get(base + '/pdf?version=' + v['hash'], headers=h).content == content
    before = len(rpc.inputs)
    assert '"status": "completed"' in ask(c, h, mode='translate', translation_source='layout').text
    assert len(rpc.inputs) == before
    app.state.store.set_document(P1, {**original, 'hash': 'f'*16})
    assert c.get(base + '/pdf?version=' + v['hash'], headers=h).content == content
    assert c.post(base + '/select-source', headers=h, json={'version': original['hash']}).status_code == 200
    assert app.state.store.document(P1)['hash'] == original['hash']
    assert ask(c, h, expected_document_hash='f'*16).status_code == 409
    assert c.post(f'/api/library/papers/{P2}/reading', headers=h, json=pos).status_code == 409
    assert c.post(f'/api/library/papers/{P1}/reading', headers=h, json={**pos, 'zoom': 'nan'}).status_code == 400
    assert c.post(f'/api/library/papers/{P1}/rating', headers=h, json={'rating': 0, 'revision': 1}).status_code == 200
    assert c.get('/api/library?translated=true', headers=h).json()['total'] == 1
    reopened = Store(app.state.store.runtime, app.state.store.root)
    assert len(reopened.library.documents(P1)) == 4
    assert reopened.library.reading(P1)['positions'][v['hash']]['zoom'] == '1.5'


def test_backup_round_trip_validation_and_local_precedence(bridge):
    c, app, rpc, h, original = translated(bridge)
    base = f'/api/papers/{P1}'
    v = c.get(base, headers=h).json()['pdf_versions'][1]
    pos = {'version': v['hash'], 'page': 2, 'rx': .2, 'ry': .7, 'zoom': 'fit'}
    c.post(f'/api/library/papers/{P1}/rating', headers=h, json={'rating': 5, 'revision': 0})
    c.post(f'/api/library/papers/{P1}/reading', headers=h, json=pos)
    c.post(base+'/annotations', headers=h, json={'document_hash': v['hash'], 'revision': 0, 'items': [mark()]})
    response = c.get('/api/library/backup', headers=h)
    assert response.status_code == 200
    archive = response.content
    with zipfile.ZipFile(BytesIO(archive)) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert 'trusted_browsers' not in manifest['tables'] and 'messages' not in manifest['tables']
        assert len(manifest['files']) == 3
    assert c.delete(f'/api/library/papers/{P1}', headers=h).status_code == 200
    assert c.get('/api/library', headers=h).json()['total'] == 0
    preview = c.post('/api/library/restore/preview', headers=h, files={'file': ('backup.zip', archive)}).json()
    assert preview['new_files'] == 3 and preview['new_papers'] == 1
    assert c.post('/api/library/restore', headers=h, json={'transfer_id': preview['transfer_id']}).status_code == 200
    data = c.get(base, headers=h).json()
    assert data['reading']['positions'][v['hash']]['ry'] == .7
    assert app.state.store.library.rating(P1)['rating'] == 5
    assert c.get(base+'/annotations?version='+v['hash'], headers=h).json()['items']
    for key, entry in manifest['files'].items():
        pid, filename = key.split('/')
        assert hashlib.sha256(app.state.store.library.path(pid, filename).read_bytes()).hexdigest() == entry['sha256']
    assert c.post(f'/api/library/papers/{P1}/rating', headers=h, json={'rating': 2, 'revision': 1}).status_code == 200
    preview = c.post('/api/library/restore/preview', headers=h, files={'file': ('backup.zip', archive)}).json()
    assert preview['new_files'] == 0
    c.post('/api/library/restore', headers=h, json={'transfer_id': preview['transfer_id']})
    assert app.state.store.library.rating(P1)['rating'] == 2
    malicious = BytesIO()
    with zipfile.ZipFile(BytesIO(archive)) as src, zipfile.ZipFile(malicious, 'w') as dst:
        for name in src.namelist():
            data = src.read(name)
            dst.writestr(name, data if name == 'manifest.json' else data + b'corrupt')
    assert c.post('/api/library/restore/preview', headers=h, files={'file': ('bad.zip', malicious.getvalue())}).status_code == 400
    assert c.post('/api/library/restore', headers=h, json={'transfer_id': '../bad'}).status_code == 422


def test_migration_keeps_versions_annotations_and_missing_files(bridge):
    c, app, _, h, original = translated(bridge)
    store = app.state.store
    old = c.get(f'/api/papers/{P1}', headers=h).json()['pdf_versions']
    with store.connect() as db:
        for table in ('library_documents', 'library_reading', 'library_papers', 'library_migrations'):
            db.execute(f'DROP TABLE {table}')
    reopened = Store(store.runtime, store.root)
    assert (store.runtime / 'before-library.sqlite3').is_file()
    assert {v['hash'] for v in reopened.library.documents(P1)} == {v['hash'] for v in old}
    reopened.library.migrate()
    assert len(reopened.library.documents(P1)) == 3
    doc = next(d for d in reopened.library.documents(P1) if d['view'] == 'translated')
    reopened.library.path(P1, doc['file']).unlink()
    assert reopened.library.listing()['papers'][0]['missing_files'] == 1
