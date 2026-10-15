"""Public-account entry, authorized presentation and responsive source figures."""
from copy import deepcopy
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def screenshot_coverage_fixture():
    return {
        'state': 'matched', 'search_status': 'blocked', 'last_search_at': '',
        'last_attempt_at': '2026-10-14T08:00:00+00:00',
        'articles': [{
            'id': 'wechat-known-crystals',
            'url': 'https://mp.weixin.qq.com/s/DXvEHDH1FQRqGQkQhBYKyw',
            'title': 'npj Comput. Mater. | 晶体生成模型的新颖稳定性能协同优化：基于强化学习的未知材料主动探索研究',
            'account': 'Ai赋能材料', 'published_at': '2026-07-22',
            'match_status': 'verified', 'display_mode': 'source',
            'verification_method': 'manual_screenshot', 'body_status': 'challenge',
            'verified_at': '2026-10-14T08:00:00+00:00',
        }],
    }


def original_fixture():
    return {'figure_id': 'original-one', 'asset': 'assets/figures/original-one.png',
            'title': 'Experiment and results', 'figure_label': 'Fig. 2',
            'caption': 'Independent validation under several loading conditions.',
            'source_url': 'https://doi.org/10.1234/example', 'credit': 'The authors',
            'license': 'CC BY 4.0', 'license_url': 'https://creativecommons.org/licenses/by/4.0/'}


def multiple_source_fixture():
    coverage = screenshot_coverage_fixture()
    coverage['articles'].extend([
        {**coverage_fixture()['articles'][0], 'body_status': 'readable',
         'title': '人工智能与增材制造：方法设计、工业验证和未来挑战的论文介绍' * 5},
        {**coverage_fixture()['articles'][0], 'url': 'https://mp.weixin.qq.com/s/another',
         'title': '另一篇已核验的论文介绍', 'published_at': '2026-10-10', 'body_status': 'readable'},
        {**coverage_fixture()['articles'][0], 'url': 'https://mp.weixin.qq.com/s/pending',
         'title': '尚未核验的相关主题', 'match_status': 'pending'},
    ])
    coverage['candidates'] = [{'url': 'https://mp.weixin.qq.com/s/candidate', 'title': '待审核线索'}]
    coverage['search_providers'] = {'sogou': {'state': 'blocked'}}
    return coverage


def mirror_fixture(**updates):
    return {'url': 'https://hub.baai.ac.cn/view/53949', 'kind': 'article',
            'body_status': 'readable', 'title': '让分子“越学越会设计”：渐进式语言模型驱动的药物结构优化',
            **updates}


def test_readable_mirror_is_a_separate_direct_link_before_conversation():
    paper = sample(); paper.update(analysis_status='ready', analysis_basis='abstract')
    coverage = screenshot_coverage_fixture()
    coverage['articles'][0]['mirrors'] = [mirror_fixture()]
    soup = BeautifulSoup(paper_card(paper, 'core', coverage=coverage), 'html.parser')
    actions = [node.get_text(strip=True) for node in soup.select('.paper-tools > *')]
    assert actions.index('公众号介绍') < actions.index('阅读全文（公开同步）') < actions.index('Codex 对话')
    assert soup.select_one('.coverage-link')['href'] == coverage['articles'][0]['url']
    mirror = soup.select_one('.coverage-mirror')
    assert mirror['href'] == mirror_fixture()['url'] and mirror['target'] == '_blank'
    assert 'noopener' in mirror['rel'] and not soup.select('iframe, .paper-coverage-panel')


@pytest.mark.parametrize('mirror', [
    mirror_fixture(kind='collection'), mirror_fixture(kind='feed'), mirror_fixture(kind=''),
    mirror_fixture(body_status='challenge'), mirror_fixture(body_status='unavailable'),
    mirror_fixture(url='https://synapse.zhihuiya.com/organization/b53ea946752160f3c870a15e059df21f'),
    mirror_fixture(url='javascript:alert(1)'),
    mirror_fixture(url='https://hub.baai.ac.cn.evil.example/view/53949'),
    mirror_fixture(url='https://hub.baai.ac.cn@evil.example/view/53949'),
    mirror_fixture(url='https://cloud.tencent.com/developer/search/article-drug'),
    mirror_fixture(url='https://hub.baai.ac.cn/view/53949?redirect=https://evil.example'),
    'not a mirror', None,
])
def test_only_safe_readable_full_articles_provide_mirror_shortcuts(mirror):
    coverage = coverage_fixture()
    coverage['articles'][0]['mirrors'] = [mirror]
    entry, panel = coverage_panel('abc', coverage)
    assert 'coverage-mirror' not in entry + panel
    assert BeautifulSoup(entry, 'html.parser').a['href'] == coverage['articles'][0]['url']


def test_mirror_shortcut_uses_verified_source_and_each_source_keeps_its_own_links():
    coverage = multiple_source_fixture()
    coverage['articles'][0]['mirrors'] = [mirror_fixture(), mirror_fixture()]
    coverage['articles'][1]['mirrors'] = [mirror_fixture(kind='collection')]
    coverage['articles'][2]['mirrors'] = [mirror_fixture(url='https://cloud.tencent.com/developer/article/2656311')]
    coverage['articles'][3]['mirrors'] = [mirror_fixture(url='https://hub.baai.ac.cn/view/99999')]
    entry, panel = coverage_panel('abc', coverage)
    soup = BeautifulSoup(entry + panel, 'html.parser')
    assert soup.select_one('.coverage-link')['href'] == coverage['articles'][1]['url']
    # The preferred WeChat article has a collection page only; the next readable
    # article still offers a one-click full-text route, without changing primary.
    assert BeautifulSoup(entry, 'html.parser').select_one('.coverage-mirror')['href'] == coverage['articles'][2]['mirrors'][0]['url']
    rows = soup.select('.coverage-list li')
    assert not rows[0].select('.coverage-mirror')
    assert rows[1].select_one('.coverage-mirror')['href'] == coverage['articles'][2]['mirrors'][0]['url']
    assert len(rows[2].select('.coverage-mirror')) == 1
    assert '99999' not in entry + panel


def test_mirror_title_escaping_and_no_content_copy():
    coverage = coverage_fixture()
    coverage['articles'][0]['mirrors'] = [mirror_fixture(title='<script>untrusted()</script>"',
        blocks=[{'type': 'paragraph', 'text': 'PRIVATE MIRROR BODY'}], images=['https://example.test/private.png'])]
    entry, panel = coverage_panel('abc', coverage)
    soup = BeautifulSoup(entry + panel, 'html.parser')
    assert not soup.select('script, iframe, img') and 'PRIVATE MIRROR BODY' not in entry + panel
    assert soup.select_one('.coverage-mirror')['title'] == '<script>untrusted()</script>"'


def test_single_source_opens_original_in_one_click_before_conversation():
    paper = sample(); paper.update(analysis_status='ready', analysis_basis='abstract')
    coverage = coverage_fixture()
    html = paper_card(paper, 'core', coverage=coverage)
    soup = BeautifulSoup(html, 'html.parser')
    actions = [n.get_text(strip=True) for n in soup.select('.paper-tools > *')]
    assert [v.split('（')[0].strip('＋') for v in actions] == ['阅读原文', '摘要解读', '公众号介绍', 'Codex 对话']
    link = soup.select_one('.coverage-link')
    assert link.name == 'a' and link['href'] == coverage['articles'][0]['url']
    assert link['target'] == '_blank' and 'noopener' in link['rel']
    assert '结构研究' in link['title']
    assert not soup.select('.coverage-toggle, .paper-coverage-panel, .coverage-candidates')
    assert coverage['articles'][0]['summary'] not in html


@pytest.mark.parametrize('root', ['./', '../'])
def test_authorized_body_opens_internal_article_and_empty_reprint_falls_back(root):
    coverage = coverage_fixture()
    row = coverage['articles'][0]
    row['display_mode'] = 'reprint'
    entry, panel = coverage_panel('abc', coverage, root=root)
    assert BeautifulSoup(entry, 'html.parser').a['href'] == row['url']
    row['blocks'] = [{'type': 'paragraph', 'text': '获准转载的正文'}]
    entry, panel = coverage_panel('abc', coverage, root=root)
    link = BeautifulSoup(entry, 'html.parser').a
    assert link['href'] == f'{root}wechat/{article_name(row)}.html'
    assert not link.has_attr('target') and not panel


@pytest.mark.parametrize('state', ['pending', 'deferred', 'no_match', 'pending_match',
                                  'unavailable', 'configuration_missing', 'error', 'matched'])
def test_without_verified_source_only_noninteractive_pending_hint_is_rendered(state):
    coverage = {'state': state, 'search_status': 'blocked',
                'last_search_at': '2026-10-15T03:00:00+00:00',
                'search_providers': {'sogou': {'state': 'blocked'}},
                'candidates': [{'url': 'https://mp.weixin.qq.com/s/candidate', 'title': '候选文章'}]}
    before = deepcopy(coverage)
    entry, panel = coverage_panel('abc', coverage)
    soup = BeautifulSoup(entry, 'html.parser')
    assert soup.select_one('.coverage-pending').get_text() == '公众号待补充'
    assert soup.select_one('.coverage-pending')['aria-disabled'] == 'true'
    assert not soup.select('button, a, [tabindex]') and not panel
    assert '访问受限' not in entry and '最近检索' not in entry and '候选文章' not in entry
    assert coverage == before


@pytest.mark.parametrize('state', ['pending', 'deferred', 'no_match', 'pending_match',
                                  'unavailable', 'configuration_missing', 'error'])
def test_unverified_paper_has_explicit_search_link_not_an_introduction(state):
    paper = sample()
    coverage = {'state': state, 'articles': [{**coverage_fixture()['articles'][0],
                                             'match_status': 'pending'}],
                'candidates': [{'title': '无关主题推荐', 'url': 'https://mp.weixin.qq.com/s/candidate'}]}
    before = deepcopy(coverage)
    entry, panel = coverage_panel(paper['id'], coverage, paper=paper)
    soup = BeautifulSoup(entry, 'html.parser')
    link = soup.select_one('a.coverage-search')
    assert link.get_text() == '搜索公众号'
    assert parse_qs(urlsplit(link['href']).query) == {'type': ['2'], 'query': [paper['title']]}
    assert link['target'] == '_blank' and 'noopener' in link['rel']
    assert not soup.select('.coverage-link, .coverage-pending') and not panel
    assert '公众号介绍' not in entry and '无关主题推荐' not in entry
    assert coverage == before


@pytest.mark.parametrize('paper,query', [
    ({'title': 'Agentic4PDM: agents & tools + "RUL" / 寿命预测? #1',
      'title_zh': '生产维护与人工智能'}, 'Agentic4PDM: agents & tools + "RUL" / 寿命预测? #1'),
    ({'title': '  ', 'title_zh': '面向“增材制造”的寿命预测：方法 A&B + 验证'},
     '面向“增材制造”的寿命预测：方法 A&B + 验证'),
    ({'title': '<script>alert("x")</script> & "paper"'}, '<script>alert("x")</script> & "paper"'),
])
def test_search_query_preserves_title_and_encodes_chinese_and_punctuation(paper, query):
    entry, panel = coverage_panel('paper', paper=paper)
    soup = BeautifulSoup(entry, 'html.parser')
    link = soup.select_one('.coverage-search')
    parts = urlsplit(link['href'])
    assert parts.scheme == 'https' and parts.netloc == 'weixin.sogou.com'
    assert parts.path == '/weixin' and not parts.fragment
    assert parse_qs(parts.query) == {'type': ['2'], 'query': [query]}
    assert link['aria-label'] == '搜索公众号：' + query
    assert not soup.select('script') and not panel


def test_home_history_and_reading_page_share_the_same_specific_search_url():
    paper = sample()
    paper.update(title='Agentic4PDM: Multi-agent Predictive Maintenance & Tool Use',
                 article=article_fixture(), analysis_status='ready', analysis_basis='full_text')
    pages = [render({'core': [paper]}), render({'core': [paper]}, archive_date='2026-10-14'),
             render_article(paper)]
    urls = []
    for html in pages:
        soup = BeautifulSoup(html, 'html.parser')
        link = soup.select_one('.coverage-search')
        urls.append(link['href'])
        assert not soup.select('.coverage-pending, .coverage-link')
        if soup.select_one('.paper-tools'):
            actions = [node.get_text(strip=True) for node in soup.select('.paper-tools > *')]
            assert actions.index('搜索公众号') < actions.index('Codex 对话')
    assert len(set(urls)) == 1
    assert parse_qs(urlsplit(urls[0]).query)['query'] == [paper['title']]


def test_confirmed_article_keeps_original_destination_when_search_metadata_is_available():
    paper = sample()
    coverage = coverage_fixture()
    entry, panel = coverage_panel(paper['id'], coverage, paper=paper)
    soup = BeautifulSoup(entry, 'html.parser')
    assert soup.select_one('.coverage-link')['href'] == coverage['articles'][0]['url']
    assert soup.select_one('.coverage-link').get_text() == '公众号介绍'
    assert not soup.select('.coverage-search') and not panel


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://evil.example/paper',
    'https://mp.weixin.qq.com.evil.example/s/paper', 'https://mp.weixin.qq.com@evil.example/s/paper',
    'https://mp.weixin.qq.com:1234/s/paper', 'https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha',
    'https://weixin.sogou.com/weixin?query=paper'])
def test_only_verified_wechat_article_urls_are_direct_entries(url):
    coverage = coverage_fixture()
    coverage['articles'][0]['url'] = url
    entry, panel = coverage_panel('abc', coverage)
    assert 'coverage-link' not in entry and '公众号待补充' in entry and not panel


def test_successful_source_remains_a_direct_link_during_refresh_failure():
    coverage = screenshot_coverage_fixture()
    coverage.update(last_attempt_state='deferred', deferred_reason='runtime_limit')
    entry, panel = coverage_panel('c53c3b080d69', coverage)
    link = BeautifulSoup(entry, 'html.parser').a
    assert link['href'] == coverage['articles'][0]['url']
    assert 'Ai赋能材料' in link['title'] and '2026-07-22' in link['title']
    assert '访问验证' in link['title'] and not panel
    assert '最近检索' not in entry and '额度' not in entry


def test_multiple_sources_prefer_readable_article_and_only_list_verified_sources():
    coverage = multiple_source_fixture()
    entry, panel = coverage_panel('abc', coverage)
    soup = BeautifulSoup(entry + panel, 'html.parser')
    assert soup.select_one('.coverage-link')['href'] == coverage['articles'][1]['url']
    assert soup.select_one('.coverage-toggle').get_text(strip=True) == '更多来源＋'
    assert soup.select_one('.paper-coverage-panel').has_attr('hidden')
    assert len(soup.select('.coverage-list li')) == 3
    assert '尚未核验的相关主题' not in panel and '待审核线索' not in panel
    assert 'search-provider' not in panel and '访问受限' not in panel
    assert coverage['articles'][1]['summary'] not in panel


def test_authorized_reprint_has_priority_over_readable_external_source():
    coverage = multiple_source_fixture()
    row = coverage['articles'][0]
    row.update(display_mode='reprint', blocks=[{'type': 'paragraph', 'text': '获准转载的正文'}])
    entry, panel = coverage_panel('abc', coverage, root='../')
    assert BeautifulSoup(entry, 'html.parser').a['href'] == f'../wechat/{article_name(row)}.html'


def test_identical_canonical_sources_do_not_create_a_more_sources_button():
    coverage = coverage_fixture()
    coverage['articles'].append({**coverage['articles'][0], 'url': 'http://mp.weixin.qq.com/s/example?tracking=1'})
    entry, panel = coverage_panel('abc', coverage)
    assert BeautifulSoup(entry, 'html.parser').a['href'] == coverage['articles'][0]['url']
    assert '更多来源' not in entry and not panel


def test_source_titles_are_escaped_in_direct_link_and_source_list():
    coverage = multiple_source_fixture()
    coverage['articles'][1]['title'] = '<script>untrusted()</script>"'
    entry, panel = coverage_panel('abc', coverage)
    soup = BeautifulSoup(entry + panel, 'html.parser')
    assert not soup.select('script') and '<script>' not in entry + panel
    assert '<script>untrusted()</script>"' in soup.select_one('.coverage-link')['title']


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('multiple', [False, True])
def test_direct_click_and_optional_source_list_on_home_and_history(browser, width, multiple):
    paper = sample(); paper.update(analysis_status='ready', analysis_basis='abstract')
    coverage = multiple_source_fixture() if multiple else screenshot_coverage_fixture()
    coverage['articles'][1 if multiple else 0]['mirrors'] = [mirror_fixture()]
    expected_url = coverage['articles'][1 if multiple else 0]['url']
    home = render({'core': [paper]}, coverage={paper['id']: coverage})
    archive = render({'core': [paper]}, archive_date='2026-10-14', coverage={paper['id']: coverage})
    context = browser.new_context(viewport={'width': width, 'height': 920})

    def route(request):
        parsed = urlsplit(request.request.url)
        path = parsed.path
        if parsed.hostname == 'mp.weixin.qq.com':
            request.fulfill(body='<h1>公众号原文</h1>', content_type='text/html')
        elif parsed.hostname == 'hub.baai.ac.cn':
            request.fulfill(body='<h1>公开同步全文</h1>', content_type='text/html')
        elif '/archive/' in path:
            request.fulfill(body=archive, content_type='text/html')
        elif path.endswith('index.html'):
            request.fulfill(body=home, content_type='text/html')
        elif '/assets/' in path:
            file = ROOT / 'tools/assets' / path.split('/assets/', 1)[1]
            if file.is_file():
                request.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
            else:
                request.abort()
        else:
            request.abort()

    context.route('**/*', route)
    page = context.new_page()
    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        for path in ('index.html', 'archive/2026-10-14.html'):
            page.goto(BASE + path, wait_until='networkidle')
            link = page.locator('.coverage-link')
            assert link.get_attribute('href') == expected_url
            with page.expect_popup() as opened:
                link.click()
            original = opened.value
            original.wait_for_load_state()
            assert original.url == expected_url
            original.close()
            mirror = page.locator('.paper-tools > .coverage-mirror')
            assert mirror.get_attribute('href') == mirror_fixture()['url']
            with page.expect_popup() as opened:
                mirror.focus(); page.keyboard.press('Enter')
            synchronized = opened.value
            synchronized.wait_for_load_state()
            assert synchronized.url == mirror_fixture()['url']
            synchronized.close()
            assert not page.locator('.coverage-candidates, .coverage-search-status').count()
            actions = page.locator('.paper-tools > *').all_text_contents()
            assert next(i for i, text in enumerate(actions) if '公众号介绍' in text) < next(i for i, text in enumerate(actions) if 'Codex 对话' in text)
            assert actions.index('阅读全文（公开同步）') < next(i for i, text in enumerate(actions) if 'Codex 对话' in text)
            if multiple:
                assert not page.locator('.paper-coverage-panel').is_visible()
                page.locator('.coverage-more').focus(); page.keyboard.press('Enter')
                assert page.locator('.coverage-list li:visible').count() == 3
                assert coverage['articles'][1]['title'] in page.locator('.coverage-list').inner_text()
                assert page.locator('.coverage-more').get_attribute('aria-expanded') == 'true'
                assert page.locator('.coverage-list .coverage-mirror:visible').count() == 1
            else:
                assert not page.locator('.coverage-toggle, .paper-coverage-panel').count()
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth + 1')
        assert not errors
    finally:
        context.close()


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
    assert json.loads((out/'paper-coverage.json').read_text(encoding='utf-8'))['version'] == 3
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
    source = screenshot_coverage_fixture()
    card=render({'core':[p]}, coverage={p['id']:source})
    archive=render({'core':[p]}, archive_date='2026-10-14', coverage={p['id']:source})
    context=browser.new_context(viewport={'width':width,'height':920})
    def route(r):
        path=urlsplit(r.request.url).path
        if '/readings/' in path:r.fulfill(body=html,content_type='text/html')
        elif '/archive/' in path:r.fulfill(body=archive,content_type='text/html')
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
        for path in ['index.html', 'archive/2026-10-14.html']:
            page.goto(BASE+path,wait_until='networkidle')
            link = page.locator('.coverage-link')
            assert link.get_attribute('href') == source['articles'][0]['url']
            assert source['articles'][0]['title'] in link.get_attribute('title')
            assert 'Ai赋能材料 · 2026-07-22' in link.get_attribute('title')
            assert not page.locator('.coverage-toggle, .paper-coverage-panel').count()
            actions = page.locator('.paper-tools > *').all_text_contents()
            assert next(i for i, text in enumerate(actions) if '公众号介绍' in text) < next(i for i, text in enumerate(actions) if 'Codex 对话' in text)
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
