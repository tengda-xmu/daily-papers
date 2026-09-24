"""Optional browser regression checks; run with Playwright and Chromium installed."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
import pymupdf as fitz
import pytest
import uvicorn

playwright = pytest.importorskip('playwright.sync_api')
from connectors.codex_bridge.documents import parse_pdf
from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN
from tests.test_codex_bridge import FakeCodex, login, P1, P2
from tests.test_pdf_annotations import source_pdf, mark
from tools.build_site import render
from src import custom_journals

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = PUBLIC_ORIGIN + '/daily-papers/'


@contextmanager
def running_app(app, monkeypatch):
    import connectors.codex_bridge.server as module
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    monkeypatch.setattr(module,'PORT',port)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    try:
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=20) as client:
            yield client
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()


@pytest.fixture(scope='module')
def browser():
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')
        instance=p.chromium.launch(args=['--disable-web-security'])
        yield instance
        instance.close()


@pytest.fixture
def reader(tmp_path,monkeypatch,browser):
    payload={'core':[{'id':P1,'title':'Paper A','abstract':'First paper'}],
             'extended':[{'id':P2,'title':'Paper B','abstract':'Second paper'}]}
    (tmp_path/'data').mkdir();(tmp_path/'data/daily.json').write_text(json.dumps(payload),encoding='utf-8')
    app=create_app(tmp_path,rpc=FakeCodex())
    original=parse_pdf(source_pdf(),app.state.store.directory(P1));app.state.store.set_document(P1,original)
    alternative=parse_pdf(source_pdf()+b'\n% second version\n',app.state.store.directory(P1));app.state.store.set_document(P1,alternative)
    app.state.store.set_document(P1,original)
    other=parse_pdf(source_pdf(),app.state.store.directory(P2));app.state.store.set_document(P2,other)
    with patch.object(custom_journals,'load_custom_journals',custom_journals.published_journals):
        html=render(payload)
    with running_app(app,monkeypatch) as client:
        headers=login(client,app)
        context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
        context.add_init_script("sessionStorage.setItem('daily-papers-codex-session',"+json.dumps(headers['Authorization'][7:])+");")
        requests=[];errors=[]
        def route(r):
            url=r.request.url
            if url.startswith(LOCAL_ORIGIN+'/api/'):
                requests.append((r.request.method,urlsplit(url).path))
                r.continue_(url=str(client.base_url).rstrip('/')+url.split(LOCAL_ORIGIN,1)[1]);return
            if url.startswith(PUBLIC+'assets/'):
                path=ROOT/'tools/assets'/url[len(PUBLIC+'assets/'):].split('?')[0]
                if path.is_file():r.fulfill(path=str(path));return
            if url.split('?')[0]==PUBLIC:r.fulfill(content_type='text/html',body=html);return
            r.abort()
        context.route('**/*',route)
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(PUBLIC)
        page.locator(f'.codex-entry[data-paper-id="{P1}"]').click()
        playwright.expect(page.locator('.reader-page').first).to_be_visible(timeout=20000)
        playwright.expect(page.locator('[data-tool="note"]')).to_be_enabled()
        def saved():
            return client.get(f'/api/papers/{P1}/annotations?version='+original['hash'],headers=headers).json()
        yield page,context,client,headers,original,alternative,saved,requests,tmp_path
        assert not errors,errors
        context.close()


def note(page,text='Draft note'):
    page.locator('[data-tool="note"]').click()
    page.locator('.reader-page').first.click(position={'x':120,'y':180})
    page.locator('.reader-note textarea').last.fill(text)
    playwright.expect(page.locator('.reader-annotation-status')).to_have_text('有未保存的批注')


def test_manual_save_discard_undo_and_collapsed_controls(reader):
    page,_,_,_,_,_,saved,requests,_=reader
    note(page)
    page.wait_for_timeout(500)
    assert saved()['revision']==0
    assert not any(method=='POST' and path.endswith('/annotations') for method,path in requests)
    page.locator('[data-read="toggle-toolbar"]').click()
    playwright.expect(page.locator('[data-read="save"]')).to_be_visible()
    page.locator('[data-read="save"]').click()
    playwright.expect(page.locator('.reader-annotation-status')).to_have_text('批注已保存到本机')
    assert saved()['revision']==1 and saved()['items'][0]['note']=='Draft note'
    page.locator('.reader-note textarea').fill('Changed')
    page.locator('[data-read="toggle-toolbar"]').click()
    page.locator('[data-read="undo"]').click()
    playwright.expect(page.locator('[data-read="save"]')).to_be_disabled()
    page.locator('[data-read="redo"]').click()
    playwright.expect(page.locator('[data-read="save"]')).to_be_enabled()
    page.locator('[data-read="discard"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Draft note')
    page.locator('.reader-note textarea').fill('Still a draft')
    page.locator('[data-read="hide"]').click()
    page.locator('[data-chat-action="reader"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Still a draft')
    page.set_viewport_size({'width':390,'height':844})
    page.locator('[data-tab="pdf"]').click()
    page.locator('[data-read="toggle-toolbar"]').click()
    playwright.expect(page.locator('[data-read="save"]')).to_be_visible()
    playwright.expect(page.locator('[data-read="discard"]')).to_be_visible()
    assert page.locator('.reader-savebar').evaluate('(el)=>el.scrollWidth<=el.clientWidth')
    page.locator('[data-read="discard"]').click()
    assert saved()['revision']==1


def test_export_draft_does_not_save_and_old_helper_does_not_fallback(reader):
    page,context,_,_,_,_,saved,requests,tmp_path=reader
    note(page,'Export only')
    with page.expect_download() as download:
        page.locator('[data-read="export"]').click()
    output=tmp_path/'draft.pdf';download.value.save_as(output)
    with fitz.open(output) as pdf:
        assert [a.info['content'] for a in pdf[0].annots()]==['Export only']
        assert 'Original paper' in pdf[0].get_text()
    assert saved()['revision']==0
    playwright.expect(page.locator('.reader-annotation-status')).to_have_text('有未保存的批注')
    context.route('**/annotated-pdf',lambda r:r.fulfill(status=405,json={'detail':'Method Not Allowed'}))
    page.locator('[data-read="export"]').click()
    playwright.expect(page.locator('.reader-status')).to_contain_text('需要更新并重启')
    assert not any(method=='POST' and path.endswith('/annotations') for method,path in requests)
    page.locator('[data-read="discard"]').click()


def test_version_switch_close_and_clear_preserve_or_discard_only_by_choice(reader):
    page,_,_,_,original,alternative,saved,_,_=reader
    note(page)
    page.locator('.reader-version').select_option(alternative['hash'])
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.reader-version')).to_have_value(original['hash'])
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Draft note')
    page.once('dialog',lambda dialog:dialog.accept())
    page.locator('[data-chat-action="clear"]').click()
    playwright.expect(page.locator('.chat-empty')).to_be_visible()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Draft note')
    assert saved()['revision']==0
    page.locator('.reader-version').select_option(alternative['hash'])
    page.locator('[data-leave="save"]').click()
    playwright.expect(page.locator('.reader-version')).to_be_enabled()
    playwright.expect(page.locator('.reader-note textarea')).to_have_count(0)
    assert saved()['revision']==1
    note(page,'Different PDF draft')
    page.locator('.reader-version').select_option(original['hash'])
    page.locator('[data-leave="discard"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Draft note')
    page.locator('.reader-note textarea').fill('Closing draft')
    page.locator('.chat-close').click()
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.paper-chat')).to_be_visible()
    page.locator('.chat-close').click()
    page.locator('[data-leave="discard"]').click()
    playwright.expect(page.locator('.paper-chat')).not_to_be_visible()
    assert saved()['items'][0]['note']=='Draft note'
    page.locator(f'.codex-entry[data-paper-id="{P1}"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Draft note')


def test_save_failure_conflict_and_inflight_edits_keep_drafts(reader):
    page,context,client,headers,doc,_,saved,_,_=reader
    note(page,'Initial draft')
    context.route('**/annotations',lambda r:r.fulfill(status=503,json={'detail':'Save unavailable'}) if r.request.method=='POST' else r.fallback())
    page.locator('[data-read="save"]').click()
    playwright.expect(page.locator('.reader-annotation-status')).to_contain_text('Save unavailable')
    assert saved()['revision']==0
    page.locator('.chat-close').click()
    page.locator('[data-leave="save"]').click()
    playwright.expect(page.locator('.reader-leave-error')).to_contain_text('Save unavailable')
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Initial draft')
    context.unroute('**/annotations')
    pending=[]
    context.route('**/annotations',lambda r:pending.append(r) if r.request.method=='POST' else r.fallback())
    page.locator('[data-read="save"]').click()
    page.locator('.reader-note textarea').fill('Edited while saving')
    assert len(pending)==1
    pending[0].fallback()
    playwright.expect(page.locator('[data-read="save"]')).to_be_enabled()
    assert saved()['items'][0]['note']=='Initial draft'
    playwright.expect(page.locator('.reader-annotation-status')).to_have_text('有未保存的批注')
    context.unroute('**/annotations')
    external={'document_hash':doc['hash'],'revision':1,'items':[mark('note',points=[[.2,.3]],note='Another page')]}
    assert client.post(f'/api/papers/{P1}/annotations',headers=headers,json=external).status_code==200
    page.locator('[data-read="save"]').click()
    playwright.expect(page.locator('.reader-annotation-status')).to_contain_text('其他页面修改')
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Edited while saving')
    assert saved()['items'][0]['note']=='Another page'
    page.locator('[data-read="reload"]').click()
    page.locator('[data-leave="discard"]').click()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Another page')


def test_network_failure_and_native_reload_warning(reader):
    page,context,_,_,_,_,saved,_,_=reader
    note(page,'Keep while offline')
    context.route('**/annotations',lambda r:r.abort('internetdisconnected') if r.request.method=='POST' else r.fallback())
    page.locator('[data-read="save"]').click()
    playwright.expect(page.locator('.reader-annotation-status')).to_contain_text('未保存的修改仍保留')
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Keep while offline')
    assert saved()['revision']==0
    context.unroute('**/annotations')
    with page.expect_event('dialog') as warning:
        page.evaluate('setTimeout(()=>location.reload(),0)')
    assert warning.value.type=='beforeunload'
    warning.value.dismiss()
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Keep while offline')
    with page.expect_event('dialog') as warning:
        page.evaluate('setTimeout(()=>location.reload(),0)')
    with page.expect_navigation():
        warning.value.accept()
    page.locator(f'.codex-entry[data-paper-id="{P1}"]').click()
    playwright.expect(page.locator('[data-tool="note"]')).to_be_enabled()
    playwright.expect(page.locator('.reader-note textarea')).to_have_count(0)
    assert saved()['revision']==0


def test_switch_paper_and_replace_source_require_decision(reader):
    page,_,_,_,original,_,saved,_,_=reader
    note(page,'Paper A draft')
    # An app event may open another paper while its modal reader is active.
    page.locator(f'.codex-entry[data-paper-id="{P2}"]').dispatch_event('click')
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.chat-paper-title')).to_have_text('Paper A')
    page.locator(f'.codex-entry[data-paper-id="{P2}"]').dispatch_event('click')
    page.locator('[data-leave="save"]').click()
    playwright.expect(page.locator('.chat-paper-title')).to_have_text('Paper B')
    playwright.expect(page.locator('.reader-note textarea')).to_have_count(0)
    assert saved()['items'][0]['note']=='Paper A draft'
    page.locator(f'.codex-entry[data-paper-id="{P1}"]').dispatch_event('click')
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Paper A draft')
    page.locator('.reader-note textarea').fill('Before replacement')
    replacement={'name':'replacement.pdf','mimeType':'application/pdf','buffer':source_pdf()+b'\n% replacement\n'}
    with page.expect_file_chooser() as chooser:
        page.locator('[data-read="upload"]').click()
    chooser.value.set_files(replacement)
    page.locator('[data-leave="cancel"]').click()
    playwright.expect(page.locator('.reader-version')).to_have_value(original['hash'])
    playwright.expect(page.locator('.reader-note textarea')).to_have_value('Before replacement')
    with page.expect_file_chooser() as chooser:
        page.locator('[data-read="upload"]').click()
    chooser.value.set_files(replacement)
    page.locator('[data-leave="discard"]').click()
    playwright.expect(page.locator('.reader-version')).not_to_have_value(original['hash'])
    playwright.expect(page.locator('[data-tool="note"]')).to_be_enabled()
    playwright.expect(page.locator('.reader-note textarea')).to_have_count(0)
    assert saved()['items'][0]['note']=='Paper A draft'
