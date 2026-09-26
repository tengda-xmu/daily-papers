"""Archive management uses a paired bridge and never deletes on simple browsing."""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pw = pytest.importorskip('playwright.sync_api')
from src.editions import entries, manifest, revision
from tools.build_site import archive_index
from tests.test_edition_deletion import seed

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://tengda-xmu.github.io/daily-papers/'
BRIDGE = 'http://127.0.0.1:43127'


@pytest.fixture(scope='module')
def browser():
    with pw.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def archive_page(browser, tmp_path):
    seed(tmp_path)
    rows = entries(tmp_path)
    state = {'editions': rows, 'current_id': '3', 'revision': revision(manifest(tmp_path)), 'operation': {'state': 'idle'}}
    html = archive_index(rows, '3')
    context = browser.new_context(viewport={'width': 1440, 'height': 950})
    context.add_init_script("sessionStorage.setItem('daily-papers-codex-session', 'fixture-token')")
    calls, errors = [], []
    def route(r):
        url = r.request.url
        if url.startswith(BASE + 'assets/'):
            path = ROOT / 'tools/assets' / url[len(BASE + 'assets/'):].split('?')[0]
            if path.is_file():
                r.fulfill(path=str(path)); return
        if url.startswith(BASE + 'archive/'):
            r.fulfill(body=html, content_type='text/html'); return
        if url.startswith(BRIDGE + '/api/recommendations/editions'):
            method, body = r.request.method, r.request.post_data_json if r.request.post_data else None
            path = urlsplit(url).path
            calls.append((method, path, body))
            assert r.request.headers['authorization'] == 'Bearer fixture-token'
            if path.endswith('/delete'):
                state['operation'] = {**body, 'state': 'queued', 'message': '正在排队'}
                r.fulfill(json=state['operation']); return
            if '/operations/' in path:
                if path.endswith('/retry'):
                    state['operation']['state'] = 'running'
                r.fulfill(json=state['operation']); return
            r.fulfill(json=state); return
        r.abort()
    context.route('**/*', route)
    page = context.new_page()
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(BASE + 'archive/')
    yield page, state, calls
    assert not errors, errors
    context.close()


@pytest.mark.parametrize('width', [1440, 768, 390])
def test_multi_select_keyboard_cancel_and_responsive_layout(archive_page, width):
    page, state, calls = archive_page
    page.set_viewport_size({'width': width, 'height': 950})
    assert not calls
    assert page.locator('.archive-choice:visible').count() == 0
    page.locator('#archive-manage').focus(); page.keyboard.press('Enter')
    page.locator('#archive-controls').wait_for(state='visible')
    page.locator('.archive-day summary').click()
    assert page.locator('input[data-edition-id="3"]').is_disabled()
    page.locator('.archive-day-choice input').check()
    assert page.locator('#archive-selected').inner_text() == '已选择 2 批'
    page.locator('input[data-edition-id="1"]').uncheck()
    assert page.locator('#archive-select-all').evaluate('(e) => e.indeterminate')
    page.locator('.archive-day summary').click()
    assert page.locator('#archive-selected').inner_text() == '已选择 1 批'
    page.once('dialog', lambda d: d.dismiss())
    page.locator('#archive-delete').click()
    assert not any(method == 'POST' for method, _, _ in calls)
    page.locator('#archive-cancel').click()
    assert page.locator('#archive-controls').is_hidden()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_delete_waits_for_publication_and_reload_resumes_without_resubmission(archive_page):
    page, state, calls = archive_page
    page.locator('#archive-manage').click()
    page.locator('#archive-controls').wait_for(state='visible')
    page.locator('#archive-select-all').check()
    dialogs = []
    def confirm(d):
        dialogs.append(d.message); d.accept()
    page.once('dialog', confirm)
    page.locator('#archive-delete').click()
    page.wait_for_function("document.querySelector('#archive-operation').textContent.includes('排队')")
    assert '第 1 批' in dialogs[0] and '第 2 批' in dialogs[0] and '第 3 批' not in dialogs[0]
    assert '相关论文可重新参与推荐' in dialogs[0]
    assert len(state['editions']) == 3 and page.locator('#archive-count').inner_text() == '共 3 批归档'
    assert state['operation']['edition_ids'] == ['1', '2']
    state['operation'].update(state='failed', message='发布失败，可重试')
    page.reload()
    page.locator('#archive-retry').wait_for(state='visible')
    assert len([c for c in calls if c[1].endswith('/delete')]) == 1
    page.locator('#archive-retry').click()
    state['operation'].update(state='succeeded', message='已删除 2 批，相关论文可重新参与推荐。')
    state['editions'] = [e for e in state['editions'] if e['id'] == '3']
    page.reload()
    page.wait_for_function("document.querySelector('#archive-count').textContent === '共 1 批归档'")
    assert page.locator('#archive-controls').is_hidden()
    assert page.evaluate("localStorage.getItem('daily-papers-archive-delete')") is None
    assert len([c for c in calls if c[1].endswith('/delete')]) == 1
