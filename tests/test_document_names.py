import json
import re
from urllib.parse import unquote

from connectors.codex_bridge.document_names import paper_pdf_name
from tests.test_codex_bridge import bridge, login, P1, pdf_bytes
from tests.test_pdf_annotations import mark


def test_upload_names_original_by_paper_title_and_preserves_bytes(bridge):
    client, app, _ = bridge
    headers = login(client, app)
    paper = {'id': P1, 'title': 'Prediction of residual life using forward/inverse learning', 'title_zh': '剩余寿命预测'}
    (app.state.store.root / 'data/daily.json').write_text(json.dumps({'core': [paper]}), encoding='utf-8')
    content = pdf_bytes()
    response = client.post(f'/api/papers/{P1}/pdf', headers=headers,
                           files={'file': ('1-s2.0-S0142112326000459-main (1).pdf', content, 'application/pdf')})
    assert response.status_code == 200
    data = client.get(f'/api/papers/{P1}', headers=headers).json()
    doc = data['document']
    expected = 'Prediction of residual life using forward_inverse learning.pdf'
    assert doc['name'] == expected
    assert doc['original_name'] == '1-s2.0-S0142112326000459-main (1).pdf'
    assert data['pdf_versions'][0]['name'] == expected
    assert json.loads(app.state.store.state(P1)['document'])['name'] == expected
    response = client.get(f'/api/papers/{P1}/pdf?version=' + doc['hash'], headers=headers)
    assert response.content == content
    assert expected in unquote(response.headers['Content-Disposition'])


def test_legacy_pdf_name_changes_without_rewriting_private_records(bridge):
    client, app, _ = bridge
    headers = login(client, app)
    base = f'/api/papers/{P1}'
    content = pdf_bytes()
    assert client.post(base + '/pdf', headers=headers, files={'file': ('random.pdf', content, 'application/pdf')}).status_code == 200
    doc = app.state.store.document(P1)
    assert client.post(base + '/annotations', headers=headers,
                       json={'document_hash': doc['hash'], 'revision': 0, 'items': [mark()]}).status_code == 200
    # Simulate the old persisted format, including its original filename.
    with app.state.store.connect() as db:
        legacy = dict(doc, name='random.pdf')
        legacy.pop('original_name')
        db.execute('UPDATE papers SET document=? WHERE id=?', (json.dumps(legacy), P1))
        stored = json.loads(db.execute('SELECT content FROM library_documents WHERE paper=?', (P1,)).fetchone()[0])
        stored['name'] = 'random.pdf'
        stored.pop('original_name')
        db.execute('UPDATE library_documents SET content=? WHERE paper=?', (json.dumps(stored), P1))
    before = app.state.store.db.read_bytes()
    result = client.get(base, headers=headers).json()
    assert result['document']['name'] == 'Paper A.pdf'
    assert result['pdf_versions'][0]['name'] == 'Paper A.pdf'
    assert result['document']['hash'] == doc['hash']
    assert client.get(base + '/pdf?version=' + doc['hash'], headers=headers).content == content
    annotations = client.get(base + '/annotations?version=' + doc['hash'], headers=headers).json()
    assert annotations['revision'] == 1 and len(annotations['items']) == 1
    assert app.state.store.db.read_bytes() == before


def test_names_handle_chinese_windows_characters_and_long_titles():
    assert paper_pdf_name({'title': '中文论文：疲劳/可靠性?'}) == '中文论文：疲劳_可靠性_.pdf'
    assert paper_pdf_name({'title': '  ', 'title_zh': '中文题名'}) == '中文题名.pdf'
    assert paper_pdf_name({'title': 'CON'}) == '论文-CON.pdf'
    long_name = paper_pdf_name({'title': '疲劳研究' * 100})
    assert long_name.endswith('.pdf') and len(long_name.encode('utf-8')) <= 234
    assert not re.search(r'[<>:"/\\|?*\x00-\x1f]', long_name)


def test_translated_versions_keep_their_distinct_names(bridge):
    from connectors.codex_bridge.document_names import name_original_pdf
    translated = {'kind': 'pdf', 'view': 'translated', 'name': '中文译文 PDF'}
    assert name_original_pdf(translated, {'title': 'Paper A'}) == translated
