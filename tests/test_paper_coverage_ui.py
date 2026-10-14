"""Public-account entry, authorized presentation and responsive source figures."""
from copy import deepcopy
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
import pytest

from tests.test_companion_reconnect_browser import browser
from tests.test_reading_articles import article_fixture
from tests.test_reading_queue import sample
from tools.build_site import paper_card, render
from tools.coverage_pages import article_name, build_coverage, coverage_panel, render_reprint
from tools.reading_articles import render_article, diagram_svg

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://tengda-xmu.github.io/daily-papers/'


def coverage_fixture():
    return {'state': 'matched', 'checked_at': '2026-10-13T03:00:00+00:00', 'articles': [{
        'id': 'wechat-example', 'url': 'https://mp.weixin.qq.com/s/example',
        'title': '物理约束与疲劳寿命预测：独立验证的方法', 'account': '结构研究',
        'published_at': '2026-10-12T00:00:00+00:00', 'summary': '这篇介绍围绕论文的方法设计和实验验证展开，可结合论文原文阅读。',
        'match_status': 'verified', 'display_mode': 'source',
    }]}


def original_fixture():
    return {'figure_id': 'original-one', 'asset': 'assets/figures/original-one.png',
            'title': 'Experiment and results', 'figure_label': 'Fig. 2',
            'caption': 'Independent validation under several loading conditions.',
            'source_url': 'https://doi.org/10.1234/example', 'credit': 'The authors',
            'license': 'CC BY 4.0', 'license_url': 'https://creativecommons.org/licenses/by/4.0/'}


def test_action_order_sources_and_archive_paths():
    paper = sample(); paper.update(analysis_status='ready', analysis_basis='abstract')
    coverage = coverage_fixture()
    html = paper_card(paper, 'core', coverage=coverage)
    soup = BeautifulSoup(html, 'html.parser')
    actions = [n.get_text(strip=True) for n in soup.select('.paper-tools > *')]
    assert [v.split('（')[0].strip('＋') for v in actions] == ['阅读原文', '摘要解读', '公众号介绍', 'Codex 对话']
    assert soup.select_one('.paper-coverage-panel').has_attr('hidden')
    assert soup.select_one('.coverage-list a')['href'] == coverage['articles'][0]['url']
    assert '站内阅读' not in html
    coverage['articles'][0]['display_mode'] = 'reprint'
    html = paper_card(paper, 'extended', root='../', coverage=coverage)
    assert f'../wechat/{article_name(coverage["articles"][0])}.html' in html


@pytest.mark.parametrize('state,text', [('pending','尚未检索'), ('deferred','待检索'),
    ('no_match','暂未检索到'), ('unavailable','无法读取'), ('configuration_missing','搜索暂不可用')])
def test_states_do_not_claim_absence_of_coverage(state, text):
    _, panel = coverage_panel('abc', {'state': state})
    assert text in panel
    assert '没有公众号' not in panel


@pytest.mark.parametrize('reason,text', [
    ('budget_exhausted', '待重新检索'),
    ('runtime_limit', '上次检索尚未完成'),
    ('', '待检索'),
])
def test_queue_explains_delay_without_claiming_a_search(reason, text):
    _, panel = coverage_panel('abc', {
        'state': 'deferred', 'queued_at': '2026-10-14T03:00:00+00:00',
        'deferred_reason': reason, 'checked_at': '', 'last_attempt_at': '',
    })
    assert text in panel and '将在更新时继续' in panel
    assert '额度' not in panel and '6 次' not in panel and '次日' not in panel
    assert '入队时间 2026-10-14 11:00' in panel
    assert '最近检索' not in panel


def test_legacy_queue_timestamp_is_not_presented_as_a_search():
    _, panel = coverage_panel('abc', {
        'state': 'deferred', 'checked_at': '2026-10-14T03:00:00+00:00',
    })
    assert '最近检索' not in panel
    assert '入队时间 2026-10-14 11:00' in panel


def test_partial_search_and_no_match_keep_actual_search_timestamp():
    coverage = {'state': 'deferred', 'deferred_reason': 'runtime_limit',
                'checked_at': '2026-10-14T03:00:00+00:00',
                'last_attempt_at': '2026-10-14T03:00:00+00:00',
                'queued_at': '2026-10-13T03:00:00+00:00'}
    _, panel = coverage_panel('abc', coverage)
    assert '最近检索 2026-10-14 11:00' in panel
    assert '入队时间 2026-10-13 11:00' in panel
    coverage['state'] = 'no_match'
    _, panel = coverage_panel('abc', coverage)
    assert '暂未检索到可确认的公众号介绍' in panel
    assert '最近检索 2026-10-14 11:00' in panel
    assert '入队时间' not in panel and '排队中' not in panel


@pytest.mark.parametrize('legacy', [False, True])
def test_queued_refresh_keeps_confirmed_source_and_explains_delay(legacy):
    coverage = coverage_fixture()
    coverage.update(last_attempt_state='deferred', deferred_reason='budget_exhausted',
                    queued_at='2026-10-14T03:00:00+00:00')
    if not legacy:
        coverage['last_attempt_at'] = coverage['checked_at']
    _, panel = coverage_panel('abc', coverage)
    assert '待重新检索' in panel and '额度' not in panel
    assert '已保留上次确认的介绍' in panel
    assert coverage['articles'][0]['url'] in panel and 'coverage-list' in panel
    assert ('最近检索 2026-10-13 11:00' in panel) is not legacy
    assert '入队时间 2026-10-14 11:00' in panel


def test_pending_sources_cannot_be_presented_as_confirmed_articles():
    c = coverage_fixture(); c['articles'][0]['match_status'] = 'pending'
    c['state'] = 'pending_match'; c['candidates'] = [c['articles'][0]]
    _, html = coverage_panel('abc', c)
    assert 'coverage-list' not in html and '待核实' in html and '候选来源' in html


def test_reprint_and_figures_escape_external_markup():
    row = coverage_fixture()['articles'][0]
    row['title'] += '<script>alert(1)</script>'
    row['blocks'] = [{'type':'paragraph', 'text':'<script>alert(2)</script>'},
                     {'type':'heading','text':'实验与验证'},
                     {'type':'image','src':'https://evil.example/tracker.png','alt':'bad'},
                     {'type':'image','src':'assets/wechat/'+'a'*64+'.png','alt':'图1'}]
    html = render_reprint(row)
    assert '<script>alert' not in html and 'evil.example' not in html
    assert html.count('class="article-original-figure"') == 1
    p=sample(); p.update(article=article_fixture(), analysis_basis='full_text')
    p['article']['figure_groups']=[{'section':'validation','title':'原图证据', 'panels':[
        {'figure_id':'original-one','caption':'试验比较<script>bad</script>'},
        {'figure_id':'nonexistent','caption':'不能发布'}]}]
    html = render_article(p, figures=[original_fixture()])
    assert '不能发布' not in html and '<script>bad' not in html
    assert '原图证据' in html and 'Fig. 2' in html and '原文图注与出处' in html


def test_revocation_removes_generated_page_and_current_index(tmp_path, monkeypatch):
    import src.paper_coverage as coverage_module
    state = {sample()['id']: coverage_fixture()}
    row=state[sample()['id']]['articles'][0]; row.update(display_mode='reprint', blocks=[{'type':'paragraph','text':'获准转载的正文'}])
    monkeypatch.setattr(coverage_module, 'load_public', lambda root: deepcopy(state))
    monkeypatch.setattr(coverage_module, 'publish_assets', lambda *a: None)
    out=tmp_path/'site'; out.mkdir()
    build_coverage(tmp_path, out)
    page=out/'wechat'/(article_name(row)+'.html')
    assert page.exists()
    row['display_mode']='source'; row.pop('blocks')
    build_coverage(tmp_path, out)
    assert not page.exists() and '获准转载的正文' not in (out/'paper-coverage.json').read_text(encoding='utf-8')


@pytest.mark.parametrize('width', [390, 1440])
def test_coverage_keyboard_and_original_groups_responsive(browser, width):
    p=sample(); p.update(article=article_fixture(),analysis_status='ready',analysis_basis='full_text',title_zh='物理约束与疲劳寿命预测')
    p['article']['figure_groups']=[{'section':'validation','title':'实验与结果', 'panels':[
        {'figure_id':'original-one','caption':'保留原始坐标和图例，核对独立试件的实验结果。'},
        {'figure_id':'original-two','caption':'对照相同工况下的基线方法。'}]}]
    figures=[original_fixture(),{**original_fixture(),'figure_id':'original-two'}]
    html=render_article(p, figures=figures, coverage=coverage_fixture())
    card=render({'core':[p]}, coverage={p['id']:{
        'state':'deferred', 'deferred_reason':'budget_exhausted',
        'queued_at':'2026-10-14T03:00:00+00:00', 'checked_at':'', 'last_attempt_at':'',
    }})
    context=browser.new_context(viewport={'width':width,'height':920})
    def route(r):
        path=urlsplit(r.request.url).path
        if '/readings/' in path:r.fulfill(body=html,content_type='text/html')
        elif path.endswith('index.html'):r.fulfill(body=card,content_type='text/html')
        elif '/reading-diagrams/' in path:
            d=next(d for d in p['article']['diagrams'] if d['kind']==('proposal' if '-proposal' in path else 'study'))
            r.fulfill(body=diagram_svg(d,mobile='-mobile' in path),content_type='image/svg+xml')
        elif path.endswith('original-one.png'):
            import io
            from PIL import Image
            buffer=io.BytesIO();Image.new('RGB',(800,500),'white').save(buffer,format='PNG')
            r.fulfill(body=buffer.getvalue(),content_type='image/png')
        elif '/assets/' in path:
            file=ROOT/'tools/assets'/path.split('/assets/',1)[1]
            if file.is_file():r.fulfill(body=file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            else:r.abort()
        else:r.abort()
    context.route('**/*',route)
    page=context.new_page(); errors=[]; page.on('pageerror', lambda e: errors.append(str(e)))
    try:
        page.goto(BASE+'index.html',wait_until='networkidle')
        page.locator('.coverage-toggle').focus();page.keyboard.press('Enter')
        assert page.locator('.paper-coverage-panel').is_visible()
        assert page.locator('.coverage-toggle').get_attribute('aria-expanded')=='true'
        panel_text = page.locator('.paper-coverage-panel').inner_text()
        assert '待重新检索' in panel_text and '额度' not in panel_text
        assert '入队时间' in panel_text and '最近检索' not in panel_text
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
        page.goto(BASE+'readings/'+p['id']+'.html',wait_until='networkidle')
        assert page.locator('.article-original-figure').count()==2
        boxes=page.locator('.article-original-figure').evaluate_all('(nodes)=>nodes.map(n=>({x:n.getBoundingClientRect().x,y:n.getBoundingClientRect().y}))')
        assert (boxes[0]['x']==boxes[1]['x']) == (width==390)
        assert not page.evaluate('document.documentElement.scrollWidth>innerWidth+1')
        page.locator('.article-original-figure .article-zoom').first.focus();page.keyboard.press('Enter')
        assert page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
        assert page.locator('.article-figure-dialog img').get_attribute('src').endswith('original-one.png')
        page.keyboard.press('Escape');assert not page.locator('.article-figure-dialog').evaluate('(d)=>d.open')
        assert not errors
    finally:
        context.close()
