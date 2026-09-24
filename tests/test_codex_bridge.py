import asyncio
from io import BytesIO
import json
import re
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf import PdfReader
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from connectors.codex_bridge.documents import ArticleParser, fetch_pdf, pdf_candidates, page_selection, parse_pdf, reading_batches, source_context, validate_url
from connectors.codex_bridge.rpc import CodexClient, CodexError, launch_args, subprocess_environment
from connectors.codex_bridge.server import LOCAL_ORIGIN, PUBLIC_ORIGIN, create_app
from tools.build_site import render
from connectors.codex_bridge.translation import translation_batches
from connectors.codex_bridge.pdf_export import export_pdf

P1, P2 = '123456789abc', 'abcdef123456'


class FakeCodex:
    version, model, images = 'test', 'test-model', True

    def __init__(self):
        self.inputs = []
        self.threads = []
        self.model_inputs = []
        self.models = [{'id': 'test-model', 'label': 'Test model', 'is_default': True, 'images': True},
                       {'id': 'other-model', 'label': 'Other model', 'is_default': False, 'images': False}]

    resolve_model = CodexClient.resolve_model

    async def refresh_models(self):
        pass

    async def start(self):
        pass

    async def close(self):
        pass

    async def call(self, method, params):
        return {'account': {'type': 'chatgpt'}}

    async def thread(self, existing=None, *, model=None):
        value = existing or str(uuid.uuid4())
        self.threads.append(value)
        return value

    async def turn(self, thread, text, images=(), *, model=None):
        self.inputs.append((thread, text, images))
        self.model_inputs.append(model)
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


def screenshot_bytes(format='PNG'):
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
    output = BytesIO()
    metadata = PngInfo(); metadata.add_text('private-metadata', 'must not be retained')
    Image.new('RGB', (96, 64), '#17496f').save(output, format=format, pnginfo=metadata)
    return output.getvalue()


def upload_image(client, headers, paper=P1, **changes):
    file = changes.pop('file', ('screenshot.png', screenshot_bytes(), 'image/png'))
    return client.post(f'/api/papers/{paper}/screenshots', files={'file': file}, headers=headers, **changes)


def test_screenshot_upload_is_private_paper_scoped_and_persistent(bridge):
    from PIL import Image
    from connectors.codex_bridge.store import Store
    c, app, _ = bridge
    assert upload_image(c, {}).status_code == 401
    h = login(c, app)
    assert upload_image(c, {**h, 'Origin': 'https://evil.example'}).status_code == 403
    response = upload_image(c, h, file=('../../secret.png', screenshot_bytes(), 'image/png'))
    assert response.status_code == 200
    item = response.json()
    assert item['name'] == 'secret.png' and set(item) == {'id', 'name', 'width', 'height'}
    url = f'/api/papers/{P1}/screenshots/{item["id"]}'
    assert c.get(url).status_code == 401
    preview = c.get(url, headers=h)
    assert preview.status_code == 200 and preview.headers['content-type'] == 'image/png'
    assert not Image.open(BytesIO(preview.content)).info
    assert c.get(f'/api/papers/{P2}/screenshots/{item["id"]}', headers=h).status_code == 400
    assert c.get(f'/api/papers/{P1}', headers=h).json()['screenshots'] == [item]
    assert Store(app.state.store.runtime, app.state.store.root).screenshots(P1, pending=True) == [item]
    assert c.delete(url, headers=h).status_code == 200
    assert c.get(url, headers=h).status_code == 400


def test_screenshot_validation_limits_and_non_image_payloads(bridge, monkeypatch):
    c, app, _ = bridge; h = login(c, app)
    for data in (b'<svg onload="alert(1)"></svg>', b'not an image', b''):
        assert upload_image(c, h, file=('fake.png', data, 'image/png')).status_code == 400
    from connectors.codex_bridge.screenshots import MAX_IMAGE_BYTES
    assert upload_image(c, h, file=('large.png', b'x' * (MAX_IMAGE_BYTES + 1), 'image/png')).status_code == 400
    monkeypatch.setattr('connectors.codex_bridge.screenshots.MAX_IMAGE_PIXELS', 1)
    assert upload_image(c, h).status_code == 400
    monkeypatch.setattr('connectors.codex_bridge.screenshots.MAX_IMAGE_PIXELS', 16_000_000)
    for _ in range(4): assert upload_image(c, h).status_code == 200
    assert upload_image(c, h).status_code == 400
    assert len(app.state.store.screenshots(P1, pending=True)) == 4


def test_screenshots_sent_as_real_images_and_saved_with_message(bridge):
    c, app, rpc = bridge; h = login(c, app)
    items = [upload_image(c, h).json() for _ in range(2)]
    response = ask(c, h, attachment_ids=[x['id'] for x in items], mode='figure')
    assert response.status_code == 200 and '"status": "completed"' in response.text
    assert len(rpc.inputs[-1][2]) == 2
    assert all(path.is_file() and path.suffix == '.png' for path in rpc.inputs[-1][2])
    assert '[上传截图1]' in rpc.inputs[-1][1]
    history = c.get(f'/api/papers/{P1}', headers=h).json()
    assert history['history'][0]['attachments'] == items and history['screenshots'] == []
    assert c.delete(f'/api/papers/{P1}/screenshots/{items[0]["id"]}', headers=h).status_code == 400
    paths = list(rpc.inputs[-1][2])
    assert c.delete(f'/api/papers/{P1}', headers=h).status_code == 200
    assert not any(path.exists() for path in paths)
    assert not app.state.store.screenshots(P1)


def test_screenshot_translation_needs_no_pdf_and_never_silently_ignored(bridge):
    c, app, rpc = bridge; h = login(c, app)
    item = upload_image(c, h).json()
    assert ask(c, h, mode='translate', translation_source='image').status_code == 400
    assert ask(c, h, mode='translate', translation_source='full', attachment_ids=[item['id']]).status_code == 400
    assert ask(c, h, mode='translate', translation_source='text', attachment_ids=[item['id']]).status_code == 400
    result = ask(c, h, mode='translate', translation_source='image', attachment_ids=[item['id']], message='翻译截图中的文字')
    assert '"status": "completed"' in result.text
    assert len(rpc.inputs[-1][2]) == 1 and '本轮上传截图中的可见文字' in rpc.inputs[-1][1]
    assert app.state.store.document(P1) is None


def test_screenshot_checks_before_model_use_and_does_not_replace_pdf(bridge):
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, {'kind': 'html', 'name':'Article', 'hash':'abc', 'pages':[], 'page_count':0, 'scan_pages':0})
    item = upload_image(c, h).json()
    assert ask(c, h, paper=P2, attachment_ids=[item['id']]).status_code == 400
    assert ask(c, h, attachment_ids=['../.env']).status_code == 400
    assert ask(c, h, attachment_ids=[item['id']] * 2).status_code == 400
    assert ask(c, h, attachment_ids=[item['id']] * 5).status_code == 422
    assert ask(c, h, attachment_ids=[item['id']], model='other-model').status_code == 400
    assert not rpc.inputs and not app.state.store.history(P1)
    assert app.state.store.document(P1)['hash'] == 'abc'
    assert len(app.state.store.screenshots(P1, pending=True)) == 1


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


def test_model_selection_is_validated_pinned_and_saved_with_history(bridge):
    c, app, rpc = bridge; h = login(c, app)
    assert c.post('/api/connect').status_code == 401
    data = c.post('/api/connect', headers=h).json()
    assert [m['id'] for m in data['models']] == ['test-model', 'other-model']
    assert ask(c, h, model='not-in-account').status_code == 400
    assert not rpc.inputs and not app.state.store.history(P1)
    ask(c, h, model='other-model')
    ask(c, h)
    assert rpc.model_inputs == ['other-model', 'test-model']
    assert rpc.threads[0] == rpc.threads[1]
    rows = c.get(f'/api/papers/{P1}', headers=h).json()['history']
    assert [r['model'] for r in rows if r['role'] == 'assistant'] == ['other-model', 'test-model']


def test_summary_batches_all_use_the_selected_model(bridge):
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, {'kind': 'html', 'hash': 'test', 'page_count': 0, 'scan_pages': 0,
        'pages': [{'label': f'S{i}', 'text': 'evidence ' * 4000, 'scan': False} for i in (1, 2)]})
    response = ask(c, h, mode='summary', model='other-model')
    assert '"status": "completed"' in response.text
    assert rpc.model_inputs == ['other-model'] * 3


def test_rpc_model_catalog_pagination_and_image_capabilities(tmp_path):
    async def run():
        client = CodexClient(tmp_path)
        calls = []
        async def call(method, params, timeout=45):
            calls.append((method, params))
            if method == 'model/list':
                if not params.get('cursor'):
                    return {'data': [{'model': 'text-only', 'inputModalities': ['text']},
                                     {'model': 'hidden', 'hidden': True}], 'nextCursor': 'page2'}
                return {'data': [{'id': 'default', 'isDefault': True}], 'nextCursor': None}
            if method == 'turn/start':
                assert params['model'] == 'text-only'
                assert params['effort'] == 'low'
                next(iter(client.listeners)).put_nowait({'method': 'turn/completed', 'params': {
                    'threadId': 'test', 'turn': {'id': 'turn-test', 'status': 'completed'}}})
                return {'turn': {'id': 'turn-test'}}
        client.call = call
        await client.refresh_models()
        assert [m['id'] for m in client.models] == ['text-only', 'default']
        assert client.model == 'default' and client.images
        assert calls[1][1]['cursor'] == 'page2'
        assert not client.resolve_model('text-only')['images']
        client.resolve_model('text-only')['efforts'] = ['low']
        with pytest.raises(CodexError, match='不支持图片'):
            async for _ in client.turn('test', 'test', [tmp_path/'image.png'], model='text-only'):
                pass
        assert all(method == 'model/list' for method, _ in calls)
        assert not client.listeners
        events = [e async for e in client.turn('test', 'test', model='text-only')]
        assert events[-1]['status'] == 'completed'
    asyncio.run(run())


@pytest.mark.parametrize('version,supported', [
    ('0.154.0-alpha.6.2', True),
    ('0.155.0-alpha.16', True),
    ('0.155.0-alpha.16.3', True),
    ('0.155.0-alpha.16.4', False),
    ('0.155.0-alpha.16.3-unverified', False),
    ('0.156.0', False),
    ('', False),
])
def test_rpc_start_only_launches_verified_builds(tmp_path, monkeypatch, version, supported):
    # Exercise the startup boundary: an unknown binary must never start the
    # app-server or read the account, even if its version has a known prefix.
    spawns, calls = [], []

    class Process:
        returncode = None
        stdin = SimpleNamespace(write=lambda _: None)

        async def communicate(self):
            return f'codex-cli {version}\n'.encode(), b''

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        spawns.append(args)
        return Process()

    async def call(method, params, timeout=45):
        calls.append(method)
        if method == 'account/read':
            return {'account': {'type': 'chatgpt'}}
        if method == 'model/list':
            return {'data': [{'model': 'test-model', 'isDefault': True}]}
        return {}

    async def read():
        pass

    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    monkeypatch.setattr('connectors.codex_bridge.rpc.executable', lambda: 'codex-test')
    monkeypatch.setattr('connectors.codex_bridge.rpc.asyncio.create_subprocess_exec', spawn)

    async def run():
        client = CodexClient(tmp_path)
        client.call, client._read = call, read
        if not supported:
            with pytest.raises(CodexError, match='尚未验证'):
                await client.start()
            assert len(spawns) == 1 and spawns[0] == ('codex-test', '--version')
            assert not calls and client.process is None
            return
        try:
            await client.start()
            assert client.version == version and client.model == 'test-model'
            assert calls == ['initialize', 'account/read', 'model/list']
            assert len(spawns) == 2
            assert 'default_permissions="paper-reader"' in spawns[1]
            assert 'permissions.paper-reader.network.enabled=false' in spawns[1]
            await client.start()
            assert len(spawns) == 2
        finally:
            await client.close()
            await client.reader
    asyncio.run(run())


@pytest.mark.parametrize('existing', [None, 'existing-thread'])
@pytest.mark.parametrize('changes,valid', [
    ({}, True),
    ({'activePermissionProfile': {'id': 'other'}}, False),
    ({'activePermissionProfile': None}, False),
    ({'sandbox': None}, False),
    ({'sandbox': {'type': 'workspaceWrite'}}, False),
    ({'sandbox': {'type': 'readOnly', 'networkAccess': True}}, False),
    ({'approvalPolicy': 'on-request'}, False),
])
def test_thread_start_and_resume_require_effective_reader_permissions(tmp_path, existing, changes, valid):
    async def run():
        client = CodexClient(tmp_path)
        client.models = FakeCodex().models; client.model = 'test-model'

        async def start():
            pass

        async def call(method, params, timeout=45):
            assert method == ('thread/resume' if existing else 'thread/start')
            assert params['permissions'] == 'paper-reader'
            assert params['approvalPolicy'] == 'never'
            return {'thread': {'id': 'existing-thread'},
                    'activePermissionProfile': {'id': 'paper-reader'},
                    'sandbox': {'type': 'readOnly', 'networkAccess': False},
                    'approvalPolicy': 'never', **changes}

        client.start, client.call = start, call
        if valid:
            assert await client.thread(existing) == 'existing-thread'
        else:
            with pytest.raises(CodexError):
                await client.thread(existing)
            assert not client.loaded
    asyncio.run(run())


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


def test_pdf_candidates_for_nature_arxiv_and_untrusted_urls():
    assert pdf_candidates({'oa_url': 'https://www.nature.com/articles/s44387-026-00102-5'})[0] == 'https://www.nature.com/articles/s44387-026-00102-5.pdf'
    assert pdf_candidates({'oa_url': 'https://arxiv.org/html/2608.07978v1'})[0] == 'https://arxiv.org/pdf/2608.07978v1'
    assert pdf_candidates({'doi': '10.48550/arxiv.2608.07978'})[0] == 'https://arxiv.org/pdf/2608.07978'
    assert not pdf_candidates({'pdf_url': 'file:///private.pdf', 'oa_url': 'https://attacker.example/paper.pdf'})


def test_fetch_pdf_uses_publisher_pdf_metadata_and_rejects_html(monkeypatch, tmp_path):
    calls = []
    def download(url):
        calls.append(url)
        if url.endswith('.pdf'):
            return pdf_bytes(), url
        return b'<meta name="citation_pdf_url" content="/paper.pdf">', url
    monkeypatch.setattr('connectors.codex_bridge.documents.download', download)
    doc = fetch_pdf({'oa_url': 'https://journals.plos.org/article?id=test'}, tmp_path)
    assert doc['kind'] == 'pdf' and doc['page_count'] == 1
    assert calls[-1] == 'https://journals.plos.org/paper.pdf'
    monkeypatch.setattr('connectors.codex_bridge.documents.download', lambda url: (b'<html>Sign in</html>', url))
    with pytest.raises(ValueError, match='未取得'):
        fetch_pdf({'oa_url': 'https://journals.plos.org/article?id=test'}, tmp_path)


def test_fetch_pdf_api_requires_pairing_and_preserves_existing_document(bridge, monkeypatch):
    c, app, rpc = bridge; h = login(c, app)
    path = f'/api/papers/{P1}/fetch-pdf'
    assert c.post(path).status_code == 401
    def download(paper, directory):
        assert c.get(f'/api/papers/{P1}', headers=h).json()['preparing']
        assert c.post(path, headers=h).status_code == 409
        return parse_pdf(pdf_bytes(), directory, '论文 PDF')
    monkeypatch.setattr('connectors.codex_bridge.server.fetch_pdf', download)
    response = c.post(path, headers=h)
    assert response.status_code == 200 and response.json()['page_count'] == 1
    original = app.state.store.document(P1)
    def blocked(paper, directory):
        raise ValueError('出版社暂不允许下载，请上传 PDF。')
    monkeypatch.setattr('connectors.codex_bridge.server.fetch_pdf', blocked)
    assert c.post(path, headers=h).status_code == 400
    assert app.state.store.document(P1) == original
    assert not c.get(f'/api/papers/{P1}', headers=h).json()['preparing']
    assert c.get(f'/api/papers/{P1}/source/P1', headers=h).status_code == 200
    assert not rpc.inputs


def test_failed_model_preserves_question_and_partial_reply(bridge):
    c, app, rpc = bridge; h = login(c, app)
    async def fail(thread, text, images=(), *, model=None):
        yield {'type': 'delta', 'text': '已生成部分'}
        raise CodexError('rate limit reached')
    rpc.turn = fail
    response = ask(c, h)
    assert 'quota_limited' in response.text
    rows = app.state.store.history(P1)
    assert rows[0]['role'] == 'user'
    assert rows[-1]['content'] == '已生成部分' and rows[-1]['status'] == 'failed'
    assert '额度' in rows[-1]['error']


def test_progress_precedes_answer_and_empty_result_is_not_success(bridge):
    c, app, rpc = bridge; h = login(c, app)
    async def empty(thread, text, images=(), *, model=None):
        yield {'type': 'started', 'turn_id': 'test'}
        yield {'type': 'progress', 'stage': 'analyzing', 'message': 'Codex 正在分析已提供资料'}
        yield {'type': 'completed', 'status': 'completed'}
    rpc.turn = empty
    response = ask(c, h)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert events[0]['stage'] == 'preparing' and events[0]['started_at']
    assert any(e.get('stage') == 'analyzing' for e in events)
    assert events[-1]['status'] == 'failed'
    row = app.state.store.history(P1)[-1]
    assert row['status'] == 'failed' and '未返回' in row['error']


def test_model_activity_is_distinct_from_request_ack_and_retries(bridge):
    c, app, rpc = bridge; h = login(c, app)
    async def active(thread, text, images=(), *, model=None):
        yield {'type': 'started', 'turn_id': 'test'}
        for _ in range(2):
            yield {'type': 'progress', 'stage': 'retrying', 'message': '连接重试'}
        yield {'type': 'activity'}
        yield {'type': 'delta', 'text': '原文证据 [P1]'}
        yield {'type': 'completed', 'status': 'completed'}
    rpc.turn = active
    response = ask(c, h)
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    waiting = next(e for e in events if e.get('stage') == 'waiting_model')
    assert waiting['model_activity_at'] is None
    retry = [e for e in events if e.get('stage') == 'retrying']
    assert [e['retry_count'] for e in retry] == [1, 2]
    assert all(e['model_activity_at'] is None for e in retry)
    activity = next(e for e in events if e['type'] == 'activity')
    writing = next(e for e in events if e.get('stage') == 'writing')
    assert writing['model_activity_at'] >= activity['model_activity_at'] > 0
    assert events[-1]['status'] == 'completed'


@pytest.mark.parametrize('target,language', [('zh', '中文'), ('en', '英文')])
def test_pasted_translation_only_uses_pasted_text_and_selected_language(bridge, target, language):
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, {'kind': 'pdf', 'hash': 'test', 'page_count': 300, 'scan_pages': 300,
                                    'pages': [{'label': 'P1', 'text': '', 'scan': True}]})
    response = ask(c, h, mode='translate', message='Material fatigue life is uncertain.',
                   translation_target=target, translation_source='text')
    assert '"status": "completed"' in response.text
    prompt = rpc.inputs[-1][1]
    assert f'目标语言：{language}' in prompt
    assert '[用户粘贴原文]\nMaterial fatigue life is uncertain.' in prompt
    assert 'Paper A evidence' not in prompt
    assert not rpc.inputs[-1][2]


def test_document_translation_requires_source_and_question_requests_evidence(bridge):
    c, app, rpc = bridge; h = login(c, app)
    response = ask(c, h, mode='translate', message='翻译摘要', translation_source='document')
    assert '请先获取开放全文' in response.text and not rpc.inputs
    response = ask(c, h, mode='question', message='证据是什么？')
    assert '"status": "completed"' in response.text
    assert '关键原文证据、分析依据与适用边界' in rpc.inputs[-1][1]


def test_rpc_recovers_completed_message_and_only_exposes_activity(tmp_path):
    async def run():
        client = CodexClient(tmp_path)
        client.models = FakeCodex().models; client.model = 'test-model'
        async def call(method, params, timeout=45):
            q = next(iter(client.listeners))
            messages = [
                ('item/started', {'item': {'id': 'r1', 'type': 'reasoning'}}),
                ('item/reasoning/textDelta', {'delta': 'private reasoning should never appear'}),
                ('error', {'willRetry': True, 'error': {'message': 'temporary failure'}}),
                ('item/agentMessage/delta', {'itemId': 'm1', 'delta': '已收到'}),
                ('item/completed', {'item': {'id': 'm1', 'type': 'agentMessage', 'text': '已收到完整回答'}}),
                ('item/completed', {'item': {'id': 'm2', 'type': 'agentMessage', 'text': '仅终态消息'}}),
                ('turn/completed', {'turn': {'id': 'turn-test', 'status': 'completed'}}),
            ]
            for method, data in messages:
                q.put_nowait({'method': method, 'params': {'threadId': 'test', 'turnId': 'turn-test', **data}})
            return {'turn': {'id': 'turn-test'}}
        client.call = call
        events = [e async for e in client.turn('test', 'test')]
        assert ''.join(e['text'] for e in events if e['type'] == 'delta') == '已收到完整回答仅终态消息'
        assert [e['stage'] for e in events if e['type'] == 'progress'] == ['analyzing', 'retrying']
        assert {'type': 'activity'} in events
        assert 'private reasoning' not in json.dumps(events)
        assert not client.listeners
    asyncio.run(run())


def test_codex_child_inherits_windows_proxy_without_changing_parent(monkeypatch):
    original = {'PATH': 'preserved', 'NO_PROXY': 'internal.example'}
    monkeypatch.setattr('connectors.codex_bridge.rpc.os', SimpleNamespace(name='nt', environ=original))
    monkeypatch.setattr('connectors.codex_bridge.rpc.getproxies', lambda: {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'})
    child = subprocess_environment()
    assert child['HTTP_PROXY'] == child['HTTPS_PROXY'] == 'http://127.0.0.1:7890'
    assert child['PATH'] == 'preserved'
    assert set(child['NO_PROXY'].split(',')) == {'internal.example', 'localhost', '127.0.0.1', '::1'}
    assert original == {'PATH': 'preserved', 'NO_PROXY': 'internal.example'}


@pytest.mark.parametrize('explicit', [{'https_proxy': 'http://configured.example:8080'}, {'ALL_PROXY': 'socks5://configured.example:1080'}, {'HTTPS_PROXY': ''}])
def test_explicit_proxy_preferences_take_precedence(monkeypatch, explicit):
    monkeypatch.setattr('connectors.codex_bridge.rpc.os', SimpleNamespace(name='nt', environ=explicit))
    def unexpected_lookup():
        raise AssertionError('Explicit configuration must not be replaced')
    monkeypatch.setattr('connectors.codex_bridge.rpc.getproxies', unexpected_lookup)
    child = subprocess_environment()
    assert {k: v for k, v in child.items() if k != 'NO_PROXY'} == explicit


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
        client.models = FakeCodex().models; client.model = 'test-model'
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


def translation_doc(count=3, kind='pdf', scan=False):
    return {'kind': kind, 'name': '测试全文', 'hash': 'translation-test', 'page_count': count,
            'scan_pages': count if scan else 0, 'file': 'source.pdf',
            'pages': [{'label': f'{"P" if kind == "pdf" else "S"}{i}',
                       'text': '' if scan else f'Original evidence on page {i}.', 'scan': scan} for i in range(1, count + 1)]}


def test_full_translation_batches_cover_long_pdf_and_html_without_ranking():
    for kind in ('pdf', 'html'):
        doc = translation_doc(30, kind)
        for part in doc['pages']:
            part['text'] = (part['label'] + ' important original content.\n') * 350
        batches = translation_batches(doc)
        assert all(len(b['text']) <= 6000 for b in batches)
        for part in doc['pages']:
            pieces = [b['text'] for b in batches if part['label'] in b['labels']]
            assert part['label'] in ''.join(pieces)
        joined = ''.join(b['text'] for b in batches)
        recovered = re.sub(r'\[[PS]\d+\]\n', '', joined)
        assert ''.join(recovered.split()) == ''.join(''.join(p['text'] for p in doc['pages']).split())
        assert joined.index(doc['pages'][0]['text'][:30]) < joined.index(doc['pages'][-1]['text'][:30])
    with pytest.raises(ValueError, match='全文翻译需要'):
        translation_batches(None)
    scans = translation_batches(translation_doc(7, scan=True))
    assert [b['scans'] for b in scans] == [[i] for i in range(1, 8)]


def test_full_translation_covers_all_pages_and_cache_survives_restart(bridge):
    from connectors.codex_bridge.store import Store
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, translation_doc())
    response = ask(c, h, mode='translate', translation_source='full', pages='1')
    assert '"status": "completed"' in response.text
    assert len(rpc.inputs) == 3
    assert all(f'Original evidence on page {i}.' in rpc.inputs[i-1][1] for i in range(1, 4))
    assert all('本站解读' not in prompt and 'Editorial A' not in prompt for _, prompt, _ in rpc.inputs)
    text = app.state.store.history(P1)[-1]['content']
    assert '3/3' in text and '[P1]' in text and '[P3]' in text
    assert '指定页码' not in app.state.store.history(P1)[0]['content']
    store = app.state.store
    restored = Store(store.runtime, store.root)
    with restored.connect() as db:
        saved = json.loads(db.execute('SELECT content FROM translations WHERE paper=?', (P1,)).fetchone()[0])
    assert len(saved['parts']) == 3
    response = ask(c, h, mode='translate', translation_source='full')
    assert '无需重复调用模型' in response.text and len(rpc.inputs) == 3
    assert app.state.store.history(P1)[-1]['content'] == text


def test_full_translation_resumes_after_failure_and_invalidates_changed_source(bridge):
    c, app, rpc = bridge; h = login(c, app)
    doc = translation_doc()
    app.state.store.set_document(P1, doc)
    calls = []
    async def unreliable(thread, text, images=(), *, model=None):
        calls.append(text)
        if len(calls) == 2:
            yield {'type': 'delta', 'text': '未完成的片段'}
            raise CodexError('network timeout')
        yield {'type': 'delta', 'text': '该部分的完整中文译文。'}
        yield {'type': 'completed', 'status': 'completed'}
    rpc.turn = unreliable
    ask(c, h, mode='translate', translation_source='full')
    partial = app.state.store.history(P1)[-1]
    assert partial['status'] == 'failed' and '未完成的片段' in partial['content']
    response = ask(c, h, mode='translate', translation_source='full')
    assert '"status": "completed"' in response.text
    assert len(calls) == 4 and 'page 2.' in calls[2] and 'page 3.' in calls[3]
    assert '未完成的片段' not in app.state.store.history(P1)[-1]['content']
    changed = {**doc, 'hash': 'changed-document'}
    app.state.store.set_document(P1, changed)
    ask(c, h, mode='translate', translation_source='full')
    assert len(calls) == 7
    ask(c, h, mode='translate', translation_source='full', translation_target='en')
    assert len(calls) == 10 and '目标语言：英文' in calls[-1]
    app.state.store.clear(P1)
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM translations').fetchone()[0] == 0


def test_full_translation_missing_source_and_empty_result_are_not_success(bridge):
    c, app, rpc = bridge; h = login(c, app)
    r = ask(c, h, mode='translate', translation_source='full')
    assert '全文翻译需要' in r.text and not rpc.inputs
    app.state.store.set_document(P1, translation_doc(1))
    async def empty(thread, text, images=(), *, model=None):
        yield {'type': 'completed', 'status': 'completed'}
    rpc.turn = empty
    r = ask(c, h, mode='translate', translation_source='full')
    assert '未返回译文' in r.text
    assert app.state.store.history(P1)[-1]['status'] == 'failed'


def test_full_translation_scans_are_sent_as_images(bridge, monkeypatch):
    c, app, rpc = bridge; h = login(c, app)
    app.state.store.set_document(P1, translation_doc(2, scan=True))
    monkeypatch.setattr('connectors.codex_bridge.translation.render_scan', lambda d, p, nums: [p / f'page-{n}.png' for n in nums])
    r = ask(c, h, mode='translate', translation_source='full')
    assert '"status": "completed"' in r.text
    assert [len(images) for _, _, images in rpc.inputs] == [1, 1]
    assert '扫描页' in rpc.inputs[0][1] and '无法辨认' in rpc.inputs[0][1]


def test_pdf_export_embeds_chinese_font_and_paginates_safely():
    content = '### 第 1 部分 [P1]\n\n**原文**\n\nStructural reliability: σ = 10 MPa. Error 10⁻⁶, scientiﬁc.\n\n**中文译文**\n\n结构可靠性分析。\n\n'
    content += '<img src="http://127.0.0.1/private"/> <script>never execute</script>\n\n'
    content += ('长段落不得被裁掉。 ' * 1200) + '\n\n末尾校验文本 END-OF-TRANSLATION'
    data = export_pdf({'title': '科研论文中文导出', 'doi': '10.000/test'}, [{'role': 'assistant', 'content': content, 'status': 'interrupted', 'model': 'test-model'}])
    assert data.startswith(b'%PDF-')
    reader = PdfReader(BytesIO(data)); text = '\n'.join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) > 1
    assert '结构可靠性分析' in text and 'END-OF-TRANSLATION' in text
    assert '10⁻⁶' in text and 'scientiﬁc' in text
    assert '未完成 / 部分内容' in text and 'never execute' in text
    fonts = reader.pages[0]['/Resources']['/Font'].get_object()
    assert any('/FontFile2' in f.get_object().get('/FontDescriptor', {}) for f in fonts.values())


def test_pdf_download_requires_pairing_and_scopes_message_to_paper(bridge):
    c, app, rpc = bridge
    assert c.get(f'/api/papers/{P1}/export-pdf').status_code == 401
    h = login(c, app)
    assert c.get(f'/api/papers/{P1}/export-pdf', headers=h).status_code == 400
    mid = app.state.store.message(P1, 'assistant', '中文全文译文 [P1]。')
    app.state.store.message(P1, 'user', '后续无关的问题')
    r = c.get(f'/api/papers/{P1}/export-pdf?message_id={mid}', headers=h)
    assert r.status_code == 200 and r.headers['content-type'] == 'application/pdf'
    assert 'attachment;' in r.headers['content-disposition']
    text = ''.join(p.extract_text() for p in PdfReader(BytesIO(r.content)).pages)
    assert '中文全文译文' in text and '后续无关' not in text
    assert c.get(f'/api/papers/{P2}/export-pdf?message_id={mid}', headers=h).status_code == 404
