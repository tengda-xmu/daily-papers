"""Exercise remembered-browser recovery without touching personal data or generating answers."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pw = pytest.importorskip('playwright.sync_api')
ROOT = Path(__file__).resolve().parents[1]
SITE = 'https://tengda-xmu.github.io/daily-papers/'
BRIDGE = 'http://127.0.0.1:43127'
DEVICE = 'D' * 43
PID = '123456789abc'


@pytest.fixture(scope='module')
def browser():
    with pw.sync_playwright() as p:
        channel = os.environ.get('PAPER_TEST_BROWSER_CHANNEL')
        if not channel and not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')
        browser = p.chromium.launch(channel=channel)
        yield browser
        browser.close()


class Companion:
    def __init__(self, browser, *, saved=True, chat=False):
        self.context = browser.new_context()
        self.context.grant_permissions(['local-network-access'], origin='https://tengda-xmu.github.io')
        if saved:
            self.context.add_init_script(f"localStorage.setItem('daily-papers-codex-browser', {json.dumps(DEVICE)})")
        self.online = False
        self.runtime_ready = False
        self.revoked = False
        self.calls, self.errors = [], []
        self.chat = chat
        self.context.route('**/*', self.route)
        self.page = self.context.new_page()
        self.page.on('pageerror', lambda e: self.errors.append(str(e)))
        self.page.clock.install()

    def route(self, route):
        path = urlsplit(route.request.url).path
        if route.request.url.startswith(SITE):
            asset = path.removeprefix('/daily-papers/assets/')
            if path.startswith('/daily-papers/assets/'):
                file = ROOT / 'tools/assets' / asset
                if file.is_file():
                    route.fulfill(path=str(file)); return
            scripts = '<script src="assets/paper-library.js" defer></script>'
            if self.chat:
                scripts += '<script src="assets/paper-chat.js" defer></script>'
            route.fulfill(content_type='text/html', body=f'''<!doctype html><html><head>{scripts}</head>
                <body><main id="reading"><article class="paper" data-paper-id="{PID}">
                <div class="paper-rating-slot"></div></article>
                <button class="codex-entry" data-paper-id="" data-paper-title="">Open</button>
                </main></body></html>'''); return
        if route.request.url.startswith(BRIDGE):
            if route.request.method == 'OPTIONS':
                route.fulfill(status=204); return
            self.calls.append((route.request.method, path))
            if not self.online:
                route.abort('connectionrefused'); return
            headers = {'Access-Control-Allow-Origin': 'https://tengda-xmu.github.io'}
            def reply(data, status=200):
                route.fulfill(status=status, json=data, headers=headers)
            if path == '/api/session/restore':
                if self.revoked:
                    reply({'state': 'unpaired', 'message': '授权已取消'}, 401)
                else:
                    reply({'token': 'restored-session', 'expires_at': 9999999999})
            elif route.request.headers.get('authorization') != 'Bearer restored-session':
                reply({'state': 'unpaired'}, 401)
            elif path == '/api/library/ratings':
                reply({'ratings': {PID: {'rating': 3, 'revision': 1}}})
            elif path == '/api/connect':
                if not self.runtime_ready:
                    reply({'state': 'runtime_unavailable', 'retryable': True, 'message': '连接器正在恢复'}, 503)
                else:
                    reply({'state': 'connected', 'model': 'test', 'images': True,
                           'models': [{'id': 'test', 'label': 'Test', 'images': True, 'is_default': True}]})
            else:
                reply({})
            return
        route.abort()

    def open(self):
        self.page.goto(SITE)

    def connected(self):
        self.page.wait_for_function("document.querySelector('.library-connection').hidden")

    def close(self):
        self.context.close()
        assert not self.errors, self.errors


def test_open_before_backend_automatically_recovers_and_restores_saved_browser(browser):
    app = Companion(browser)
    try:
        app.open()
        app.page.wait_for_function("document.querySelector('.library-status').dataset.error === 'true'")
        assert app.page.evaluate("localStorage.getItem('daily-papers-codex-browser')") == DEVICE
        app.online = True
        app.page.clock.run_for(17000)
        app.connected()
        assert ('POST', '/api/session/restore') in app.calls
        assert ('POST', '/api/pair') not in app.calls
        # A new tab has no short-lived session, but keeps the remembered browser.
        page = app.context.new_page()
        page.goto(SITE)
        page.wait_for_function("document.querySelector('.library-connection').hidden")
        assert app.calls.count(('POST', '/api/session/restore')) == 2
    finally:
        app.close()


def test_connection_events_are_coalesced_and_expired_session_is_restored(browser):
    app = Companion(browser)
    try:
        app.online = True
        app.open(); app.connected()
        app.page.evaluate("sessionStorage.setItem('daily-papers-codex-session','expired')")
        before = len(app.calls)
        app.page.evaluate("window.dispatchEvent(new Event('online')); window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange'))")
        app.page.wait_for_function("sessionStorage.getItem('daily-papers-codex-session') === 'restored-session'")
        app.connected()
        assert app.calls[before:].count(('POST', '/api/session/restore')) == 1
        assert not any(method != 'GET' and path != '/api/session/restore' for method, path in app.calls)
    finally:
        app.close()


def test_unpaired_browser_does_not_connect_and_revoked_authorization_stops_retry(browser):
    app = Companion(browser, saved=False)
    try:
        app.open(); app.page.clock.run_for(65000)
        assert app.calls == []
    finally:
        app.close()
    app = Companion(browser)
    try:
        app.online = True; app.revoked = True
        app.open()
        app.page.wait_for_function("localStorage.getItem('daily-papers-codex-browser') === ''")
        before = len(app.calls)
        app.page.clock.run_for(120000)
        assert len(app.calls) == before
    finally:
        app.close()


def test_runtime_recovery_keeps_pairing_and_draft_without_resending_question(browser):
    app = Companion(browser, chat=True)
    try:
        app.online = True
        app.open(); app.connected()
        app.page.locator('.codex-entry').click()
        app.page.wait_for_function("document.querySelector('.chat-status').dataset.state === 'runtime_unavailable'")
        app.page.locator('.chat-composer textarea').fill('Unsaved research question')
        app.runtime_ready = True
        app.page.clock.run_for(9000)
        app.page.wait_for_function("document.querySelector('.chat-status').dataset.state === 'connected'")
        assert app.page.locator('.chat-composer textarea').input_value() == 'Unsaved research question'
        assert ('POST', '/api/ask') not in app.calls
        assert ('POST', '/api/pair') not in app.calls
        before = app.calls.count(('POST', '/api/connect'))
        app.page.evaluate("document.dispatchEvent(new CustomEvent('paper-library-connected'))")
        app.page.clock.run_for(1000)
        assert app.calls.count(('POST', '/api/connect')) == before
    finally:
        app.close()
