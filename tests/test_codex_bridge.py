import asyncio
from io import BytesIO
import json
from pathlib import Path
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from connectors.codex_bridge.documents import ArticleParser, page_selection, parse_pdf, reading_batches, source_context, validate_url
from connectors.codex_bridge.rpc import CodexClient, CodexError, launch_args
from connectors.codex_bridge.server import LOCAL_ORIGIN, PUBLIC_ORIGIN, create_app
from tools.build_site import render

P1, P2 = '123456789abc', 'abcdef123456'


class FakeCodex:
    version, model, images = 'test', 'test-model', True

    def __init__(self):
        self.inputs = []
        self.threads = []

    async def start(self):
        pass

    async def close(self):
        pass

    async def call(self, method, params):
        return {'account': {'type': 'chatgpt'}}

    async def thread(self, existing=None):
        value = existing or str(uuid.uuid4())
        self.threads.append(value)
        return value

    async def turn(self, thread, text, images=()):
        self.inputs.append((thread, text, images))
        yield {'type': 'started', 'turn_id': 'turn-test'}
        yield {'type': 'delta', 'text': '根据原文，模型处理传感数据 [P1]。'}
        yield {'type': 'completed', 'status': 'completed'}


@pytest.fixture
def bridge(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    papers = [{'id': P1, 'title': 'Paper A', 'abstract': 'Paper A evidence', 'summary': 'Editorial A'},
              {'id': P2, 'title': 'Paper B', 'abstract': 'Paper B evidence'}]
    (data / 'daily.json').write_text(json.dumps({'core': papers[:1], 'extended': papers[1:]}), encoding='utf-8')
    rpc = FakeCodex()
    app = create_app(tmp_path, rpc=rpc)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        yield client, app, rpc


def login(client, app):
    response = client.post('/api/pair', json={'code': app.state.pair_code}, headers={'Origin': PUBLIC_ORIGIN})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token'], 'Origin': PUBLIC_ORIGIN}


def ask(client, headers, paper=P1, **changes):
    data = dict(paper_id=paper, message='这篇论文有什么证据？', mode='question', request_id=str(uuid.uuid4()))
    data.update(changes)
    return client.post('/api/ask', json=data, headers=headers)


def pdf_bytes():
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=400)
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 20 350 Td (This original paper describes sensors and a model with measured evidence.) Tj ET')
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    page[NameObject('/Contents')] = writer._add_object(stream)
    output = BytesIO(); writer.write(output)
    return output.getvalue()


def test_private_routes_pairing_origin_host_and_preflight(bridge):
    c, app, _ = bridge
    assert c.get('/api/health').json()['service'] == 'daily-papers-codex'
    assert c.get('/api/papers').status_code == 401
    assert c.post('/api/pair', json={'code': 'wrong'}).status_code == 403
    h = login(c, app)
    assert c.get('/api/papers', headers=h).status_code == 200
    assert c.get('/api/papers', headers={**h, 'Origin': 'https://attacker.example'}).status_code == 403
    assert c.get('/api/papers', headers={**h, 'Host': 'attacker.example'}).status_code == 403
    assert c.get('/api/papers', headers={'Authorization': 'Bearer invalid'}).status_code == 401
    preflight = c.options('/api/ask', headers={'Origin': PUBLIC_ORIGIN})
    assert preflight.headers['access-control-allow-origin'] == PUBLIC_ORIGIN
    assert preflight.headers['access-control-allow-private-network'] == 'true'


def test_pair_bruteforce_is_limited(bridge):
    c, app, _ = bridge
    for _ in range(8):
        assert c.post('/api/pair', json={'code': 'wrong'}).status_code == 403
    assert c.post('/api/pair', json={'code': 'wrong'}).status_code == 429


def test_pair_code_recovery_requires_session_and_local_origin(bridge):
    c, app, _ = bridge
    assert c.post('/api/pairing-code', headers={'Origin': LOCAL_ORIGIN}).status_code == 401
    h = login(c, app)
    assert c.post('/api/pairing-code', headers=h).status_code == 403
    assert c.post('/api/pairing-code', headers={'Authorization': h['Authorization']}).status_code == 403
    response = c.post('/api/pairing-code', headers={**h, 'Origin': LOCAL_ORIGIN})
    assert response.status_code == 200
    assert response.json()['code'] == app.state.pair_code
    assert response.headers['cache-control'] == 'no-store'


def test_conversations_are_scoped_resumable_and_idempotent(bridge):
    c, app, rpc = bridge; h = login(c, app)
    request_id = str(uuid.uuid4())
    response = ask(c, h, request_id=request_id)
    assert response.status_code == 200 and '"type": "done"' in response.text
    assert ask(c, h, request_id=request_id).status_code == 409
    ask(c, h); ask(c, h, paper=P2)
    assert rpc.threads[0] == rpc.threads[1] != rpc.threads[2]
    assert 'Paper B evidence' not in rpc.inputs[0][1]
    assert 'Paper A evidence' not in rpc.inputs[2][1]
    assert len(c.get(f'/api/papers/{P1}', headers=h).json()['history']) == 4
    assert len(c.get(f'/api/papers/{P2}', headers=h).json()['history']) == 2


def test_upload_grounding_references_new_thread_and_clear(bridge):
    c, app, rpc = bridge; h = login(c, app)
    ask(c, h)
    response = c.post(f'/api/papers/{P1}/pdf', files={'file': ('sample.pdf', pdf_bytes(), 'application/pdf')}, headers=h)
    assert response.status_code == 200, response.text
    ask(c, h)
    assert rpc.threads[0] != rpc.threads[1]
    assert '[P1]' in rpc.inputs[-1][1]
    assert 'measured evidence' in rpc.inputs[-1][1]
    assert '本站解读' in rpc.inputs[-1][1]
    assert c.get(f'/api/papers/{P1}/source/P1', headers=h).status_code == 200
    assert c.get(f'/api/papers/{P1}/source/P99', headers=h).status_code == 404
    assert c.get(f'/api/papers/{P1}/source/P1?version=old-version', headers=h).status_code == 409
    assert app.state.store.history(P1)[-1]['document_hash']
    ask(c, h, paper=P2)
    assert c.delete(f'/api/papers/{P1}', headers=h).status_code == 200
    assert not app.state.store.history(P1)
    assert app.state.store.history(P2)
    assert not app.state.store.document(P1)


def test_unknown_paper_and_invalid_pdf_rejected(bridge):
    c, app, rpc = bridge; h = login(c, app)
    assert ask(c, h, paper='0'*12).status_code == 400
    assert ask(c, h, paper='../.env').status_code == 422
    assert c.post(f'/api/papers/{P1}/pdf', files={'file': ('x.pdf', b'not a pdf')}, headers=h).status_code == 400
    assert not rpc.inputs


def test_failed_model_preserves_question_and_partial_reply(bridge):
    c, app, rpc = bridge; h = login(c, app)
    async def fail(thread, text, images=()):
        yield {'type': 'delta', 'text': '已生成部分'}
        raise CodexError('rate limit reached')
    rpc.turn = fail
    response = ask(c, h)
    assert 'quota_limited' in response.text
    rows = app.state.store.history(P1)
    assert rows[0]['role'] == 'user'
    assert rows[-1]['content'] == '已生成部分' and rows[-1]['status'] == 'failed'


def test_pdf_extraction_and_page_selection(tmp_path):
    doc = parse_pdf(pdf_bytes(), tmp_path)
    assert doc['page_count'] == 1 and not doc['scan_pages']
    assert 'sensors' in doc['pages'][0]['text']
    assert page_selection('1-3,5', 5) == [1, 2, 3, 5]
    with pytest.raises(ValueError): page_selection('0-2', 5)
    with pytest.raises(ValueError): page_selection('3-9', 5)


def test_scan_batches_and_full_summary_cover_every_page():
    doc = {'kind': 'pdf', 'page_count': 25, 'scan_pages': 25,
           'pages': [{'label': f'P{i}', 'text': '', 'scan': True} for i in range(1, 26)]}
    batches = reading_batches(doc, '总结', 'summary')
    assert [p for b in batches for p in b['scans']] == list(range(1, 26))
    assert all(len(b['scans']) <= 4 for b in batches)
    with pytest.raises(ValueError): reading_batches(doc, '具体问题', 'question')


def test_html_excludes_scripts_navigation_and_numbers_paragraphs():
    parser = ArticleParser()
    parser.feed('<nav>not evidence</nav><main><h2>Methods</h2><p>Measured <b>signals</b>.</p><script>bad()</script></main>')
    assert parser.parts == ['Methods', 'Measured signals.']


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'http://127.0.0.1/a', 'https://evil.example/paper', 'https://user:pass@www.nature.com/paper'])
def test_fulltext_only_known_https_hosts(url):
    with pytest.raises(ValueError): validate_url(url)


def test_dns_private_address_rejected(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError): validate_url('https://www.nature.com/paper')


def test_launch_has_independent_profile_and_disabled_tools(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'config.toml').write_text('[mcp_servers.private]\ncommand="example"\n')
    args = launch_args('codex', tmp_path)
    assert 'default_permissions="paper-reader"' in args
    assert 'features.shell_tool=false' in args
    assert 'features.apps=false' in args
    assert 'features.code_mode_host=false' in args
    assert 'mcp_servers."private".enabled=false' in args
    assert all('danger-full-access' not in arg for arg in args)


def test_site_chat_entries_current_and_archive_and_escaping():
    paper = {'id': P1, 'title': '<script>unsafe</script>', 'source': 'arXiv'}
    for archived in (None, '2026-09-23'):
        html = render({'core': [paper], 'extended': [{**paper, 'id': P2}]}, archive_date=archived)
        assert html.count('class="text-button codex-entry"') == 2
        assert '<script>unsafe</script>' not in html
        assert 'paper-chat.js?v=' in html and 'paper-chat.css?v=' in html


def test_nested_editorial_description_not_labeled_original_abstract():
    text = source_context({'abstract': 'editorial evidence', 'raw_metadata': {'raw_metadata': {'abstract_kind': 'editorial discovery description'}}})
    assert '[检索简介（非出版社原文摘要）]' in text
    assert '[摘要]' not in text


def test_rpc_interrupts_unexpected_tool_and_checks_turn_settings(tmp_path):
    async def run():
        client = CodexClient(tmp_path)
        methods = []
        async def call(method, params, timeout=45):
            methods.append(method)
            if method == 'turn/start':
                assert params['permissions'] == 'paper-reader'
                assert params['effort'] == 'medium'
                next(iter(client.listeners)).put_nowait({'method': 'item/started', 'params': {
                    'threadId': 'owned', 'item': {'type': 'commandExecution'}}})
                return {'turn': {'id': 'turn-one'}}
            return {}
        client.call = call
        with pytest.raises(CodexError):
            async for _ in client.turn('owned', 'test'):
                pass
        assert methods == ['turn/start', 'turn/interrupt']
        assert not client.listeners
    asyncio.run(run())
