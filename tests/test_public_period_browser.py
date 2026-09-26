"""Public time filters: calendar boundaries, drafts, links and browser preferences."""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pw = pytest.importorskip('playwright.sync_api')
from tools.build_site import render_leads
from tools.public_pages import render_ai

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://tengda-xmu.github.io/daily-papers/'
NOW = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)  # Sep 26, 00:30 in Beijing


@pytest.fixture(scope='module')
def browser():
    with pw.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def public_page(browser):
    dates = {'today': '2026-09-26', 'zoned-today': '2026-09-25T16:05:00Z',
             'yesterday': '2026-09-25T15:59:59Z', 'seven-edge': '2026-09-20T00:00:00+08:00',
             'before-seven': '2026-09-19T23:59:59+08:00', 'thirty-edge': '2026-08-28',
             'before-thirty': '2026-08-27', 'ninety-edge': '2026-06-29',
             'old': '2026-01-01', 'missing': '', 'invalid': '2026-02-30',
             'malformed': 'not-a-date', 'future': '2026-09-27',
             'old-funding': '2025-01-01', 'undated-conference': '', 'old-conference': '2026-01-01'}
    dates.update({f'recent-{i}': '2026-09-25' for i in range(24)})
    rows = [{'id': key, 'title': key, 'url': 'https://example.org/' + key, 'source': 'Fixture',
             'published_at': value, 'summary': 'A public announcement', 'topics': ['research'],
             'categories': ['models'], 'scenarios': ['research'], 'kind': 'news'} for key, value in dates.items()]
    for row in rows:
        if row['id'] == 'old-funding':
            row.update(kind='funding', deadline='2026-12-01', verified_at='2026-09-25', verification='verified', stage='application')
        if 'conference' in row['id']:
            row.update(kind='conference', start='2026-10-01', end='2026-10-02')
    html = {'ai.html': render_ai({'entries': rows, 'sources': []}),
            'leads.html': render_leads({}, {'entries': rows, 'sources': [], 'days': 90,
                                          'directions': [{'id': 'research', 'name': '科研'}]})}
    context = browser.new_context(viewport={'width': 1440, 'height': 1000}, timezone_id='America/Los_Angeles')
    requests, errors = [], []
    def route(r):
        url = r.request.url
        requests.append((r.request.method, url))
        if url.startswith(BASE + 'assets/'):
            path = ROOT / 'tools/assets' / url[len(BASE + 'assets/'):].split('?')[0]
            if path.is_file():
                r.fulfill(path=str(path)); return
        name = urlsplit(url).path.rsplit('/', 1)[-1]
        if url.startswith(BASE) and name in html:
            r.fulfill(content_type='text/html', body=html[name]); return
        r.abort()
    context.route('**/*', route)
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.clock.set_fixed_time(NOW)
    yield page, context, requests
    assert not errors, errors
    assert not any(method != 'GET' or '/api/' in url for method, url in requests)
    context.close()


def visible(page):
    return set(page.locator('#lead-list > li:not([hidden]) h2 a').all_text_contents())


def custom(page, value, enter=False):
    page.locator('#lead-period').select_option('custom')
    page.locator('#lead-period-days').fill(value)
    if enter:
        page.locator('#lead-period-days').press('Enter')
    else:
        page.locator('#lead-period-custom button').click()


@pytest.mark.parametrize('column,default', [('ai', '30'), ('leads', '90')])
def test_calendar_boundaries_and_strict_publication_dates(public_page, column, default):
    page, _, requests = public_page
    page.goto(BASE + column + '.html')
    pw.expect(page.locator('#lead-period')).to_have_value(default)
    assert parse_qs(urlsplit(page.url).query)['period'] == [default]
    page.locator('#lead-page-size').select_option('100')
    count = len(requests)
    page.locator('#lead-period').select_option('7')
    names = visible(page)
    assert {'today', 'zoned-today', 'yesterday', 'seven-edge'} <= names
    assert not {'before-seven', 'old-funding', 'undated-conference', 'old-conference', 'missing', 'invalid', 'future'} & names
    custom(page, '1', enter=True)
    assert visible(page) == {'today', 'zoned-today'}
    page.locator('#lead-period').select_option('30')
    assert 'thirty-edge' in visible(page) and 'before-thirty' not in visible(page)
    page.locator('#lead-period').select_option('90')
    assert 'ninety-edge' in visible(page) and 'old' not in visible(page)
    page.locator('#lead-period').select_option('all')
    assert {'missing', 'invalid', 'malformed', 'old-funding', 'undated-conference', 'old-conference'} <= visible(page)
    assert 'future' not in visible(page)
    assert len(requests) == count  # No collection, paid searches or network refreshes.
    pw.expect(page.locator('#lead-filter-toggle')).to_have_text('筛选')
    assert page.locator('#lead-filter-panel #lead-period').count() == 0


def test_custom_draft_validation_pagination_and_combined_filters(public_page):
    page, _, _ = public_page
    page.goto(BASE + 'ai.html?topic=research&period=90')
    page.locator('#lead-next').click()
    pw.expect(page.locator('#lead-page')).to_have_text('2 / 2 页')
    page.locator('#lead-period').select_option('custom')
    before = (page.url, visible(page), page.locator('#lead-count').inner_text())
    for value in ('', '0', '-1', '1.5', '3651'):
        page.locator('#lead-period-days').fill(value)
        page.locator('#lead-period-custom button').click()
        pw.expect(page.locator('#lead-period-error')).to_have_text('请输入 1–3650 的整数天数。')
        assert (page.url, visible(page), page.locator('#lead-count').inner_text()) == before
        assert page.evaluate("localStorage.getItem('daily-papers-ai-period')") is None
    page.locator('#lead-period-days').fill('45')
    assert (page.url, visible(page), page.locator('#lead-count').inner_text()) == before
    page.locator('#lead-period-days').press('Enter')
    pw.expect(page.locator('#lead-period-error')).to_be_hidden()
    pw.expect(page.locator('#lead-page')).to_have_text('1 / 2 页')
    assert parse_qs(urlsplit(page.url).query)['period'] == ['45']
    assert parse_qs(urlsplit(page.url).query)['topic'] == ['research']
    pw.expect(page.locator('#lead-filter-toggle')).to_have_text('筛选 1')
    page.locator('#lead-filter-toggle').click()
    page.locator('#lead-filter-toggle').click()
    assert parse_qs(urlsplit(page.url).query)['period'] == ['45']
    page.locator('#lead-query').fill('nonexistent')
    pw.expect(page.locator('#lead-empty')).to_be_visible()
    page.locator('#lead-reset').click()
    pw.expect(page.locator('#lead-period')).to_have_value('30')
    pw.expect(page.locator('#lead-query')).to_have_value('')
    assert page.evaluate("localStorage.getItem('daily-papers-ai-period')") is None
    custom(page, '3650')
    assert parse_qs(urlsplit(page.url).query)['period'] == ['3650']
    page.locator('#lead-next').click()
    page.locator('#lead-sort').select_option('latest')
    assert 'page' not in parse_qs(urlsplit(page.url).query)


def test_independent_preferences_url_priority_reload_and_back(public_page):
    page, _, _ = public_page
    page.goto(BASE + 'ai.html')
    custom(page, '45')
    page.goto(BASE + 'leads.html')
    pw.expect(page.locator('#lead-period')).to_have_value('90')
    page.locator('#lead-period').select_option('all')
    page.goto(BASE + 'ai.html')
    pw.expect(page.locator('#lead-period-days')).to_have_value('45')
    page.reload()
    pw.expect(page.locator('#lead-period-days')).to_have_value('45')
    page.goto(BASE + 'ai.html?period=7')
    pw.expect(page.locator('#lead-period')).to_have_value('7')
    page.go_back()
    pw.expect(page.locator('#lead-period-days')).to_have_value('45')
    page.go_forward()
    pw.expect(page.locator('#lead-period')).to_have_value('7')
    for value in ('0', '-1', '1.5', '3651', 'bad'):
        page.goto(BASE + 'ai.html?period=' + value)
        pw.expect(page.locator('#lead-period-days')).to_have_value('45')
        assert parse_qs(urlsplit(page.url).query)['period'] == ['45']
    page.goto(BASE + 'leads.html')
    pw.expect(page.locator('#lead-period')).to_have_value('all')
    page.locator('#lead-reset').click()
    pw.expect(page.locator('#lead-period')).to_have_value('90')
    assert page.evaluate("localStorage.getItem('daily-papers-leads-period')") is None
    assert page.evaluate("localStorage.getItem('daily-papers-ai-period')") == '45'


@pytest.mark.parametrize('column', ['ai', 'leads'])
def test_layout_keyboard_and_unavailable_storage(public_page, column):
    page, context, _ = public_page
    context.add_init_script("Storage.prototype.getItem=Storage.prototype.setItem=Storage.prototype.removeItem=()=>{throw new Error('Storage disabled');}")
    page.goto(BASE + column + '.html?period=45')
    for width in (1440, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        custom(page, '21', enter=True)
        assert parse_qs(urlsplit(page.url).query)['period'] == ['21']
        assert page.locator('.compact-results').evaluate('el => el.scrollWidth <= el.clientWidth')
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
        assert page.locator('#lead-period').bounding_box()['height'] == (44 if width <= 640 else 38)
        page.locator('#lead-period').select_option('all')
        pw.expect(page.locator('#lead-period-custom')).to_be_hidden()
