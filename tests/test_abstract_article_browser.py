"""Real page and refresh behavior with every network request intercepted."""
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_abstract_article_pages import abstract_paper, original_figures, study_diagram
from tests.test_companion_reconnect_browser import browser
from tests.test_figure_collection import png
from tools.build_site import render
from tools.reading_articles import diagram_svg, render_article

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://tengda-xmu.github.io/daily-papers/'


@pytest.mark.parametrize('width', [390, 1440])
def test_abstract_home_history_article_layout_and_zoom(browser, width):
    paper = abstract_paper(); paper['title_zh'] = '仅依据公开摘要理解物理信息与数据建模：研究目标、方法关系及仍待全文确认的证据边界'
    paper['article']['diagrams'] = [study_diagram()]
    paper['article']['figure_groups'] = [{'section': 'method', 'title': '论文方法原图',
        'panels': [{'figure_id': f'original-{i}', 'caption': '这幅原图保留完整坐标、图例及作者原始标注。'} for i in (1, 2)]}]
    article = render_article(paper, figures=original_figures())
    home = render({'core': [paper], 'extended': []})
    archive = render({'core': [paper], 'extended': []}, archive_date='2026-10-14')
    context = browser.new_context(viewport={'width': width, 'height': 920})
    def route(r):
        path = urlsplit(r.request.url).path
        if '/readings/' in path:
            r.fulfill(body=article, content_type='text/html')
        elif '/archive/' in path:
            r.fulfill(body=archive, content_type='text/html')
        elif path.endswith('index.html'):
            r.fulfill(body=home, content_type='text/html')
        elif '/reading-diagrams/' in path:
            r.fulfill(body=diagram_svg(study_diagram(), mobile='-mobile' in path), content_type='image/svg+xml')
        elif '/assets/figures/original-' in path:
            r.fulfill(body=png(), content_type='image/png')
        elif '/assets/' in path:
            file = ROOT/'tools/assets'/path.split('/assets/', 1)[1]
            if file.is_file():
                r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            else:
                r.abort()
        else:
            r.abort()
    context.route('**/*', route)
    page = context.new_page(); errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        destinations = []
        for path in ('index.html', 'archive/2026-10-14.html'):
            page.goto(BASE+path, wait_until='networkidle')
            link = page.locator('.reading-link')
            assert link.inner_text() == '摘要解读'
            destinations.append(link.evaluate('(n)=>n.href'))
            assert not page.locator('.paper-toggle, .paper-detail-panel').count()
            actions = page.locator('.paper-tools > *').all_text_contents()
            assert next(i for i, text in enumerate(actions) if '摘要解读' in text) < next(i for i, text in enumerate(actions) if 'Codex 对话' in text)
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
        assert destinations == [BASE+'readings/'+paper['id']+'.html']*2
        page.locator('.reading-link').click(); page.wait_for_load_state('networkidle')
        assert page.locator('.article-type').inner_text() == '摘要解读'
        assert '未核验全文细节' in page.locator('.article-updated').inner_text()
        assert page.locator('.article-original-figure').count() == 2
        assert page.locator('.article-diagram').count() == 1
        assert not page.locator('.experiment-evidence, .article-future').count()
        boxes = page.locator('.article-original-figure').evaluate_all('(nodes)=>nodes.map(n=>({x:n.getBoundingClientRect().x,y:n.getBoundingClientRect().y}))')
        assert (boxes[0]['x'] == boxes[1]['x']) == (width == 390)
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
        assert page.locator('.reading-article').evaluate('(n)=>getComputedStyle(n).fontSize') == ('16px' if width == 390 else '17px')
        assert page.locator('.reading-article').bounding_box()['width'] <= 760
        for selector, suffix in (('.article-original-figure .article-zoom', 'original-1.png'),
                                 ('.article-diagram .article-zoom', '-study-mobile.svg' if width == 390 else '-study.svg')):
            page.locator(selector).first.focus(); page.keyboard.press('Enter')
            assert page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
            assert page.locator('.article-figure-dialog img').get_attribute('src').endswith(suffix)
            page.keyboard.press('Escape')
            assert not page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
        if os.environ.get('PAPER_QA_DIR'):
            directory = Path(os.environ['PAPER_QA_DIR']); directory.mkdir(parents=True, exist_ok=True)
            page.evaluate('document.activeElement?.blur(); window.scrollTo(0, 0)')
            page.screenshot(path=str(directory/f'abstract-article-{width}.png'), full_page=True)
        assert not errors
    finally:
        context.close()


def test_published_abstract_refresh_replaces_pending_independent_link(browser):
    paper = abstract_paper()
    pending = {**paper, 'analysis_status': 'pending', 'analysis_basis': '', 'article_updated_at': '', 'analyzed_at': ''}
    pending.pop('article')
    old = render({'core': [pending]}); updated = render({'core': [paper]})
    context = browser.new_context(viewport={'width': 1440, 'height': 920})
    context.add_init_script("sessionStorage.setItem('daily-papers-codex-session', 'fixture-only')")
    calls = {'home': 0}
    def route(r):
        parsed = urlsplit(r.request.url); path = parsed.path
        if path.endswith('/api/recommendations/reading-tasks'):
            r.fulfill(json={'tasks': [{'paper_id': paper['id'], 'state': 'awaiting_fulltext', 'label': '摘要图文解读已完成',
                'completed_at': paper['article_updated_at']}], 'counts': {'published': 1, 'generating': 0}})
        elif parsed.hostname == 'tengda-xmu.github.io' and path.endswith('index.html'):
            calls['home'] += 1
            r.fulfill(body=old if calls['home'] == 1 else updated, content_type='text/html')
        elif '/assets/' in path:
            file = ROOT/'tools/assets'/path.split('/assets/', 1)[1]
            if file.is_file():
                r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            else:
                r.abort()
        else:
            r.abort()
    context.route('**/*', route)
    page = context.new_page(); errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.goto(BASE+'index.html', wait_until='networkidle')
        page.wait_for_function("document.querySelector('.reading-link')?.textContent==='摘要解读'")
        assert calls['home'] >= 2
        assert page.locator('.reading-link').get_attribute('href') == './readings/'+paper['id']+'.html'
        assert page.locator('[data-reading-status]').get_attribute('data-reading-result') == paper['article_updated_at']
        assert not errors
    finally:
        context.close()
