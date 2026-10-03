"""Real responsive article, table and diagram interactions, without a live assistant."""
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.test_companion_reconnect_browser import browser
from tests.test_reading_articles import article_fixture
from tests.test_reading_queue import sample
from tools.reading_articles import render_article, diagram_svg

ROOT=Path(__file__).resolve().parents[1]
BASE='https://tengda-xmu.github.io/daily-papers/'


@pytest.mark.parametrize('width',[1440,390])
def test_article_titles_tables_figures_and_keyboard(browser,width):
    p=sample();p.update(article=article_fixture(),title_zh='物理约束怎样支持结构疲劳寿命的可靠预测',analysis_basis='full_text')
    html=render_article(p)
    context=browser.new_context(viewport={'width':width,'height':920})
    def route(r):
        path=urlsplit(r.request.url).path
        if '/readings/' in path:r.fulfill(body=html,content_type='text/html')
        elif '/reading-diagrams/' in path:
            kind='proposal' if '-proposal' in path else 'study'
            d=next(d for d in p['article']['diagrams'] if d['kind']==kind)
            r.fulfill(body=diagram_svg(d,mobile='-mobile' in path),content_type='image/svg+xml')
        elif '/assets/' in path:
            file=ROOT/'tools/assets'/path.split('/assets/',1)[1]
            if file.is_file():r.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            else:r.abort()
        else:r.abort()
    context.route('**/*',route)
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.goto(BASE+'readings/'+p['id']+'.html',wait_until='networkidle')
        assert page.locator('h1').inner_text()==p['title_zh']
        assert page.locator('.article-original').inner_text()==p['title']
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
        assert page.locator('.experiment-evidence table').count()==1
        assert page.locator('.article-diagram').count()==2
        page.locator('.experiment-details summary').focus();page.keyboard.press('Enter')
        assert page.locator('.experiment-details').evaluate('(d)=>d.open')
        page.locator('.article-toc summary').focus();page.keyboard.press('Enter')
        assert page.locator('.article-toc').evaluate('(d)=>d.open')
        page.locator('.article-zoom').first.focus();page.keyboard.press('Enter')
        assert page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
        assert ('-mobile' in page.locator('.article-figure-dialog img').get_attribute('src')) == (width<600)
        page.keyboard.press('Escape');assert not page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
        assert not errors
    finally:context.close()
