from datetime import datetime, timedelta, timezone
import hashlib
import io
import json

import pytest
from PIL import Image

from src import paper_coverage as coverage
from src.public_sources import read, write

NOW = datetime(2026, 10, 13, 13, tzinfo=timezone.utc)
URL = 'https://mp.weixin.qq.com/s/paper-article'
IMAGE = 'https://mmbiz.qpic.cn/mmbiz_png/fixture/640'
PAPER = {'id': 'aabbccddeeff', 'doi': '10.1234/fixture',
         'title': 'Physics informed digital twins for wind turbine fault prognosis',
         'authors': ['Trishitha Tirupathi']}


@pytest.fixture(autouse=True)
def no_live_summary_service(monkeypatch):
    monkeypatch.setattr(coverage, 'model_summary', lambda *_: None)


def page(text=None, image=False):
    text = text if text is not None else '研究团队介绍了风机数字孪生的新研究方法和实验结果。' * 4 + ' DOI: 10.1234/fixture'
    return ('<h1 id="activity-name">风机数字孪生研究</h1><a id="js_name">科研公众号</a>'
            '<script>var ct="1791874800";</script><div id="js_content"><h2>研究介绍</h2><p>'
            + text + '</p>' + ('<img data-src="' + IMAGE + '" alt="实验结果">' if image else '')
            + '</div>').encode()


def setup(root, papers=None):
    write(root / 'data/daily.json', {'core': papers if papers is not None else [PAPER], 'extended': []})


def grant(root, scope=None, **changes):
    row = {'url': URL, 'status': 'authorized', 'permission_source': 'Publisher written grant 2026-10-12',
           'scope': scope if scope is not None else ['body'], 'authorized_at': (NOW - timedelta(days=1)).isoformat()}
    row.update(changes)
    write(root / coverage.PERMISSIONS, {'version': 1, 'articles': [row]})
    return row


def search(query, key):
    return {'organic_results': [{'link': URL, 'title': '搜索标题', 'snippet': 'DOI: 10.1234/fixture copied ' * 100}]}


def test_article_identity_uses_body_and_primary_links_not_search_snippets():
    article = coverage.parse_article(page(), URL)
    assert coverage.match_article(PAPER, article) == 'doi'
    assert coverage.match_article(PAPER, {'summary': '10.1234/fixture', 'title': PAPER['title']}) == ''
    linked = page('介绍研究方法。' * 30 + '<a href="https://doi.org/10.1234/fixture">阅读论文</a>')
    assert coverage.match_article(PAPER, coverage.parse_article(linked, URL)) == 'doi'
    assert coverage.match_article(PAPER, coverage.parse_article(page('相关内容。' * 40 + 'DOI: https://doi.org/10.1234/FIXTURE.'), URL)) == 'doi'


def test_exact_nature_paper_link_is_identity_evidence_in_main_introduction():
    paper = {**PAPER, 'id': 'a5695ec17eb2', 'doi': '10.1038/s41467-026-78118-3'}
    body = page('本篇研究提出新的材料设计方法。' * 10 + '<a href="https://www.nature.com/articles/s41467-026-78118-3">论文链接</a>')
    assert coverage.match_article(paper, coverage.parse_article(body, URL)) == 'doi'


@pytest.mark.parametrize('link', [
    'https://www.nature.com/articles/s41467-026-78118-4',
    'https://www.nature.com.evil.example/articles/s41467-026-78118-3',
    'https://evil.example/?url=https://www.nature.com/articles/s41467-026-78118-3',
    'https://www.nature.com/articles/s41467-026-78118-3-extra',
])
def test_wrong_or_embedded_nature_link_does_not_confirm(link):
    paper = {**PAPER, 'doi': '10.1038/s41467-026-78118-3'}
    assert coverage.match_article(paper, coverage.parse_article(page('另一篇研究。' * 20 + '<a href="' + link + '">论文链接</a>'), URL)) == ''


def test_nature_link_in_reference_section_does_not_confirm():
    paper = {**PAPER, 'doi': '10.1038/s41467-026-78118-3'}
    body = page('另一篇研究。' * 20 + '</p><h2>参考文献</h2><p><a href="https://www.nature.com/articles/s41467-026-78118-3">相关文献</a>')
    assert coverage.match_article(paper, coverage.parse_article(body, URL)) == ''


@pytest.mark.parametrize('heading', ['参考文献', 'References', '2. Bibliography'])
def test_reference_only_identity_is_not_a_confirmed_match(heading):
    body = page('这是另一篇文章。' * 20 + '</p><h2>' + heading + '</h2><p>' + PAPER['title']
                + ' Trishitha Tirupathi DOI: 10.1234/fixture')
    assert coverage.match_article(PAPER, coverage.parse_article(body, URL)) == ''


@pytest.mark.parametrize('reference', ['参考文献： DOI: 10.1234/fixture', '[1] Trishitha Tirupathi. DOI: 10.1234/fixture'])
def test_inline_reference_or_numbered_citation_does_not_match(reference):
    body = page('这是其他研究的说明。' * 20 + '</p><p>' + reference)
    assert coverage.match_article(PAPER, coverage.parse_article(body, URL)) == ''


@pytest.mark.parametrize('metadata', ['<span id="js_author_name">研究作者</span>',
    '<div id="meta_content"><span class="rich_media_meta_text">研究作者</span></div>', '<meta name="author" content="研究作者">'])
def test_original_author_metadata_is_preserved(metadata):
    article = coverage.parse_article(metadata.encode() + page(), URL)
    assert article['author'] == '研究作者'


def test_full_title_requires_author_when_doi_absent():
    text = '公众号论文介绍。' * 10 + PAPER['title']
    assert coverage.match_article(PAPER, coverage.parse_article(page(text), URL)) == ''
    assert coverage.match_article(PAPER, coverage.parse_article(page(text + ' Trishitha Tirupathi'), URL)) == 'title_author'
    assert coverage.match_article(PAPER, coverage.parse_article(page(text + ' Different Author'), URL)) == ''


def test_body_requires_actual_wechat_content():
    with pytest.raises(ValueError):
        coverage.parse_article(b'<html><h1>Login</h1></html>', URL)


def test_local_matches_skip_search_and_keep_copied_content_private(tmp_path):
    setup(tmp_path)
    write(tmp_path / 'data/social-articles.json', {'entries': [
        {'platform': 'wechat', 'url': URL, 'title': '研究', 'summary': '10.1234/fixture'}]})
    calls = []
    def no_search(*args):
        raise AssertionError('Local confirmed match should skip search')
    result = coverage.collect(tmp_path, NOW, key='', searcher=no_search,
                              fetcher=lambda url, source: calls.append(url) or page())
    article = result['papers'][PAPER['id']]['articles'][0]
    assert calls == [URL] and article['display_mode'] == 'source'
    assert '研究团队介绍了风机' not in json.dumps(result, ensure_ascii=False)
    assert 'blocks' not in article and '可点击来源链接' in article['summary']
    cached = read(tmp_path / coverage.CACHE / 'articles' / (article['id'] + '.json'))
    assert cached['raw_body'] and cached['article']['match_text']


def test_search_is_all_accounts_and_verifies_direct_article(tmp_path):
    setup(tmp_path)
    calls = []
    result = coverage.collect(tmp_path, NOW, key='private-key',
                              searcher=lambda query, key: calls.append(query) or search(query, key), fetcher=lambda *_: page())
    assert calls == ['site:mp.weixin.qq.com "10.1234/fixture"']
    item = result['papers'][PAPER['id']]
    assert item['state'] == 'matched' and item['articles'][0]['account'] == '科研公众号'
    public = json.dumps(result)
    assert 'private-key' not in public and 'copied' not in public
    assert item['articles'][0]['url'] == URL


def test_snippet_candidate_remains_unverified_when_body_is_unavailable(tmp_path):
    setup(tmp_path)
    result = coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=lambda *_: b'<html>Login</html>')
    row = result['papers'][PAPER['id']]
    assert row['state'] == 'pending_match' and row['articles'] == []
    assert row['candidates'][0]['reason'] == 'article_unavailable'


def test_unrelated_article_and_unsafe_search_results_are_not_promoted(tmp_path):
    setup(tmp_path)
    def results(*_):
        return {'organic_results': [{'link': 'https://mp.weixin.qq.com.evil.test/s/one', 'title': 'bad'},
                                   {'link': URL, 'title': 'maybe'}]}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=results,
                              fetcher=lambda *_: page('其他论文内容与引用。' * 30))
    row = result['papers'][PAPER['id']]
    assert row['state'] == 'pending_match' and not row['articles']
    assert len(row['candidates']) == 1 and row['candidates'][0]['url'] == URL


def test_no_results_missing_configuration_and_errors_are_distinct(tmp_path):
    setup(tmp_path)
    missing = coverage.collect(tmp_path, NOW, key='')
    assert missing['papers'][PAPER['id']]['state'] == 'configuration_missing'
    empty = coverage.collect(tmp_path, NOW + timedelta(days=2), key='key', searcher=lambda *_: {'organic_results': []})
    assert empty['papers'][PAPER['id']]['state'] == 'no_match'
    failed = coverage.collect(tmp_path, NOW + timedelta(days=10), key='key', searcher=lambda *_: {'error': 'private response'})
    assert failed['papers'][PAPER['id']]['state'] == 'error'
    assert 'private response' not in json.dumps(failed)


def test_all_papers_are_searched_without_a_daily_quota_and_cached(tmp_path):
    papers = [{**PAPER, 'id': f'{i:012x}', 'doi': f'10.1234/{i}', 'title': PAPER['title'] + f' variant {i}'} for i in range(5)]
    setup(tmp_path, papers)
    calls = []
    def empty(query, key):
        calls.append(query)
        return {'organic_results': []}
    before = NOW.replace(hour=15, minute=59)
    result = coverage.collect(tmp_path, before, key='key', searcher=empty)
    assert len(calls) == sum(len(coverage._queries(p)) for p in papers)
    assert {row['state'] for row in result['papers'].values()} == {'no_match'}
    coverage.collect(tmp_path, before + timedelta(seconds=15), key='key', searcher=empty)
    assert len(calls) == sum(len(coverage._queries(p)) for p in papers)
    coverage.collect(tmp_path, before + timedelta(minutes=2), key='key', searcher=empty)
    assert len(calls) == sum(len(coverage._queries(p)) for p in papers)
    assert not (tmp_path / coverage.CACHE / 'budget.json').exists()


def test_legacy_exhausted_budget_and_recent_deferral_do_not_block_search(tmp_path):
    setup(tmp_path)
    budget = {'day': NOW.astimezone(coverage.CHINA).date().isoformat(), 'count': 100000}
    write(tmp_path / coverage.CACHE / 'budget.json', budget)
    queued = NOW - timedelta(minutes=10)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {
        'state': 'deferred', 'deferred_reason': 'budget_exhausted', 'queued_at': queued.isoformat(),
        'checked_at': NOW.isoformat(), 'articles': [], 'candidates': []}}})
    calls = []
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: calls.append(1) or {'organic_results': []})['papers'][PAPER['id']]
    assert len(calls) == len(coverage._queries(PAPER)) and row['state'] == 'no_match'
    assert 'deferred_reason' not in row and row['queued_at'] == queued.isoformat()
    assert row['checked_at'] == row['last_attempt_at'] == NOW.isoformat()
    assert read(tmp_path / coverage.CACHE / 'budget.json') == budget


def test_legacy_queue_timestamp_is_migrated_and_does_not_block_same_day_retry(tmp_path):
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {
        'state': 'deferred', 'checked_at': NOW.isoformat(), 'articles': [], 'candidates': []}}})
    old = coverage.load_public(tmp_path, NOW)[PAPER['id']]
    assert old['queued_at'] == NOW.isoformat()
    assert old['checked_at'] == old['last_attempt_at'] == ''
    calls = []
    later = NOW + timedelta(minutes=10)
    row = coverage.collect(tmp_path, later, key='key', searcher=lambda query, key: calls.append(query) or {'organic_results': []})['papers'][PAPER['id']]
    assert len(calls) == len(coverage._queries(PAPER)) and row['state'] == 'no_match'
    assert row['checked_at'] == row['last_attempt_at'] == later.isoformat()
    assert row['queued_at'] == NOW.isoformat() and 'deferred_reason' not in row


def test_runtime_deferred_paper_can_run_later_the_same_day(tmp_path):
    setup(tmp_path)
    calls = []
    empty = lambda query, key: calls.append(query) or {'organic_results': []}
    row = coverage.collect(tmp_path, NOW, key='key', searcher=empty, max_seconds=0)['papers'][PAPER['id']]
    assert row['state'] == 'deferred' and row['deferred_reason'] == 'runtime_limit'
    assert row['checked_at'] == '' and not calls
    later = NOW + timedelta(minutes=10)
    row = coverage.collect(tmp_path, later, key='key', searcher=empty)['papers'][PAPER['id']]
    assert row['state'] == 'no_match' and len(calls) == len(coverage._queries(PAPER))
    assert row['checked_at'] == later.isoformat()


def test_partial_search_keeps_real_attempt_time_across_same_day_deferrals(tmp_path):
    setup(tmp_path)
    calls, ticks = [], [0]
    def empty(query, key):
        calls.append(query)
        ticks[0] += 1
        return {'organic_results': []}
    row = coverage.collect(tmp_path, NOW, key='key', searcher=empty,
                           max_seconds=1, monotonic=lambda: ticks[0])['papers'][PAPER['id']]
    assert len(calls) == 1 and row['state'] == 'deferred'
    assert row['checked_at'] == row['last_attempt_at'] == NOW.isoformat()
    assert row['deferred_reason'] == 'runtime_limit'
    later = coverage.collect(tmp_path, NOW + timedelta(hours=1), key='key', searcher=empty, max_seconds=0)['papers'][PAPER['id']]
    assert later == row and len(calls) == 1


def test_cached_searches_recover_queued_paper_without_requests_or_redating(tmp_path):
    setup(tmp_path)
    stamp = NOW - timedelta(hours=2)
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'deferred', 'checked_at': NOW.isoformat()}}})
    write(tmp_path / coverage.CACHE / 'budget.json', {'day': NOW.astimezone(coverage.CHINA).date().isoformat(), 'count': 6})
    for query in coverage._queries(PAPER):
        path = coverage.search_cache_path(tmp_path, query)
        write(path, {'version': coverage.SEARCH_VERSION, 'state': 'ok', 'checked_at': stamp.isoformat(), 'rows': []})
    def no_search(*_):
        raise AssertionError('Fresh cached searches must not send requests')
    row = coverage.collect(tmp_path, NOW, key='key', searcher=no_search)['papers'][PAPER['id']]
    assert row['state'] == 'no_match'
    assert row['checked_at'] == row['last_attempt_at'] == stamp.isoformat()
    assert read(tmp_path / coverage.CACHE / 'budget.json')['count'] == 6


def test_missing_configuration_does_not_invent_attempt_or_delay_restored_key(tmp_path):
    setup(tmp_path)
    row = coverage.collect(tmp_path, NOW, key='')['papers'][PAPER['id']]
    assert row['state'] == 'configuration_missing' and row['checked_at'] == row['last_attempt_at'] == ''
    calls = []
    later = NOW + timedelta(minutes=5)
    row = coverage.collect(tmp_path, later, key='key', searcher=lambda query, key: calls.append(query) or {'organic_results': []})['papers'][PAPER['id']]
    assert row['state'] == 'no_match' and len(calls) == len(coverage._queries(PAPER))
    assert row['checked_at'] == later.isoformat()


def test_cached_matching_body_retains_its_actual_check_time(tmp_path):
    setup(tmp_path)
    write(tmp_path / 'data/social-articles.json', {'entries': [{'url': URL, 'title': PAPER['title']}]})
    row = coverage.collect(tmp_path, NOW, key='', fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert row['checked_at'] == NOW.isoformat()
    def no_fetch(*_):
        raise AssertionError('Fresh article body must be reused')
    again = coverage.collect(tmp_path, NOW + timedelta(days=6), key='', fetcher=no_fetch)['papers'][PAPER['id']]
    assert again['state'] == 'matched'
    assert again['checked_at'] == again['last_attempt_at'] == NOW.isoformat()


def test_success_cache_seven_days_and_failure_cache_one_day(tmp_path):
    calls = []
    def empty(query, key):
        calls.append(query)
        return {'organic_results': []}
    assert coverage._search(tmp_path, 'good', NOW, 'key', empty)[1] == 'ok'
    coverage._search(tmp_path, 'good', NOW + timedelta(days=6), '', empty)
    assert calls == ['good']
    coverage._search(tmp_path, 'good', NOW + timedelta(days=8), 'key', empty)
    assert calls == ['good', 'good']
    def fail(query, key):
        calls.append(query)
        raise OSError('Do not publish me')
    coverage._search(tmp_path, 'bad', NOW, 'key', fail)
    coverage._search(tmp_path, 'bad', NOW + timedelta(hours=23), 'key', fail)
    assert calls.count('bad') == 1
    coverage._search(tmp_path, 'bad', NOW + timedelta(hours=25), 'key', fail)
    assert calls.count('bad') == 2


def test_explicit_permissions_needed_and_revocation_removes_body(tmp_path):
    setup(tmp_path)
    grant(tmp_path)
    coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=lambda *_: page(image=True))
    article = coverage.load_public(tmp_path, NOW)[PAPER['id']]['articles'][0]
    assert article['display_mode'] == 'reprint' and any(b['type'] == 'paragraph' for b in article['blocks'])
    assert not any(b['type'] == 'image' for b in article['blocks'])
    grant(tmp_path, status='revoked')
    article = coverage.load_public(tmp_path, NOW)[PAPER['id']]['articles'][0]
    assert article['display_mode'] == 'source' and 'blocks' not in article


@pytest.mark.parametrize('changes', [
    {'permission_source': ''}, {'scope': []}, {'status': 'pending'},
    {'authorized_at': ''}, {'authorized_at': '2027-01-01'}, {'expires_at': '2026-01-01'}, {'expires_at': 'invalid'}])
def test_permission_metadata_is_explicit_and_current(tmp_path, changes):
    grant(tmp_path, **changes)
    assert coverage.permissions(tmp_path, NOW) == {}


def test_authorized_image_cache_survives_publication_without_private_cache(tmp_path):
    setup(tmp_path)
    grant(tmp_path, scope=['body', 'images'])
    out = io.BytesIO()
    Image.new('RGB', (3, 2), 'red').save(out, format='PNG')
    def fetch(url, source):
        return out.getvalue() if url == IMAGE else page(image=True)
    coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=fetch)
    article = coverage.load_public(tmp_path, NOW)[PAPER['id']]['articles'][0]
    src = next(b['src'] for b in article['blocks'] if b['type'] == 'image')
    site = tmp_path / 'site'
    assert coverage.publish_assets(tmp_path, site, NOW) == {src}
    (tmp_path / coverage.CACHE / 'assets' / src.rsplit('/', 1)[-1]).unlink()
    assert coverage.publish_assets(tmp_path, site, NOW) == {src}
    assert (site / src).is_file()
    grant(tmp_path, status='revoked')
    assert coverage.publish_assets(tmp_path, site, NOW) == set()
    assert not (site / src).exists()


def test_unapproved_remote_images_and_html_are_never_projected(tmp_path):
    grant(tmp_path, scope=['body', 'images'])
    article = {'url': URL, 'title': '<script>bad</script>article', 'account': '<b>account</b>',
               'match_status': 'verified', 'match_basis': 'doi', 'blocks': [
                   {'type': 'html', 'html': '<script>bad</script>'},
                   {'type': 'paragraph', 'text': '<b>Allowed text</b>'},
                   {'type': 'image', 'src': 'https://evil.test/img.png'},
                   {'type': 'image', 'src': 'assets/wechat/../../secret.png'}]}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [article]}}})
    output = coverage.load_public(tmp_path, NOW)[PAPER['id']]['articles'][0]
    assert output['blocks'] == [{'type': 'paragraph', 'text': 'Allowed text'}]
    assert '<' not in json.dumps(output)


def test_prior_verified_success_survives_unavailable_refresh(tmp_path):
    setup(tmp_path)
    write(tmp_path / 'data/social-articles.json', {'entries': [{'url': URL, 'title': PAPER['title']} ]})
    coverage.collect(tmp_path, NOW, key='', fetcher=lambda *_: page())
    def unavailable(*_):
        raise OSError('private connection details')
    result = coverage.collect(tmp_path, NOW + timedelta(days=8), key='', fetcher=unavailable)
    row = result['papers'][PAPER['id']]
    assert row['state'] == 'matched' and len(row['articles']) == 1
    assert row['last_attempt_state'] == 'unavailable'
    assert row['candidates'] == []


def test_existing_matches_are_searched_again_after_seven_days(tmp_path):
    setup(tmp_path)
    calls = []
    def discover(query, key):
        calls.append(query)
        url = URL if len(calls) == 1 else 'https://mp.weixin.qq.com/s/new-report'
        return {'organic_results': [{'link': url, 'title': '刊文'}]}
    coverage.collect(tmp_path, NOW, key='key', searcher=discover, fetcher=lambda *_: page())
    coverage.collect(tmp_path, NOW + timedelta(days=6), key='key', searcher=discover, fetcher=lambda *_: page())
    assert len(calls) == 1
    result = coverage.collect(tmp_path, NOW + timedelta(days=8), key='key', searcher=discover, fetcher=lambda *_: page())
    assert len(calls) == 2 and len(result['papers'][PAPER['id']]['articles']) == 2


def test_original_summary_is_grounded_cached_and_public(tmp_path):
    setup(tmp_path)
    calls = []
    summary = '公众号讨论了一项面向风电机组的建模工作：把机理知识引入数字化模型，用于设备故障预测，并结合实验呈现方法效果。具体技术细节与实验条件仍需查阅论文。'
    def summarize(article, paper):
        calls.append(article['url'])
        return {'summary': summary, 'evidence': ['研究团队介绍了风机数字孪生的新研究方法和实验结果。']}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=lambda *_: page(), summarizer=summarize)
    article = result['papers'][PAPER['id']]['articles'][0]
    assert article['summary_status'] == 'ready' and article['summary'] == summary
    assert len(article['summary_content_version']) == 64 and 'evidence' not in article
    coverage.collect(tmp_path, NOW + timedelta(hours=1), key='key', searcher=search, fetcher=lambda *_: page(), summarizer=summarize)
    assert calls == [URL]


def test_copied_or_ungrounded_summary_stays_pending(tmp_path):
    setup(tmp_path)
    def copied(article, paper):
        return {'summary': '研究团队介绍了风机数字孪生的新研究方法和实验结果。' * 3,
                'evidence': ['研究团队介绍了风机数字孪生的新研究方法和实验结果。']}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=lambda *_: page(), summarizer=copied)
    article = result['papers'][PAPER['id']]['articles'][0]
    assert article['summary_status'] == 'pending' and '概述待补充' in article['summary']
    assert '研究团队介绍了风机' not in json.dumps(result, ensure_ascii=False)
    parsed = coverage.parse_article(page(), URL)
    assert coverage._valid_summary({'summary': '原创摘要' * 15, 'evidence': ['正文没有这句话']}, parsed) == ''


def test_latest_tiers_precede_history_and_doi_enrichment_retains_identity(tmp_path):
    from src.editions import relative_path
    earlier = {**PAPER, 'doi': '', 'id': '001122334455'}
    archived = {**PAPER, 'id': '111122223333', 'doi': '10.1234/other', 'title': 'Another distinct historical paper title'}
    entry = {'id': 'edition-one', 'date': '2026-10-01', 'number': 1, 'generated_at': '2026-10-01T13:00:00Z'}
    write(tmp_path / 'data/editions/index.json', {'schema': 2, 'editions': [entry]})
    write(tmp_path / 'data' / relative_path(entry), {'core': [earlier, archived], 'extended': []})
    latest = {**PAPER, 'id': 'abcdef123456'}
    extended = {**PAPER, 'id': 'ccccddddeeee', 'doi': '10.1234/extended', 'title': 'A new extended paper with another title'}
    write(tmp_path / 'data/daily.json', {'core': [latest], 'extended': [extended]})
    rows = coverage.selected_papers(tmp_path)
    assert [r['id'] for r in rows] == [earlier['id'], extended['id'], archived['id']]
    assert rows[0]['doi'] == PAPER['doi']


def test_index_only_local_candidate_has_search_entry_and_no_article_claim(tmp_path):
    setup(tmp_path)
    write(tmp_path / 'data/social-articles.json', {'entries': [
        {'platform': 'wechat', 'title': PAPER['title'], 'account': '某研究号',
         'url': 'https://weixin.sogou.com/weixin?type=2&query=test', 'evidence_kind': 'search_snippet'}]})
    result = coverage.collect(tmp_path, NOW, key='')
    row = result['papers'][PAPER['id']]
    assert not row['articles'] and row['state'] == 'pending_match'
    assert row['search_status'] == 'configuration_missing'
    assert row['candidates'][0]['reason'] == 'index_only'
    assert row['candidates'][0]['url'].startswith('https://weixin.sogou.com/weixin?type=2&query=')


def test_asset_hash_must_match_before_preserving_existing_public_file(tmp_path):
    grant(tmp_path, scope=['body', 'images'])
    src = 'assets/wechat/' + 'a' * 64 + '.png'
    row = {'url': URL, 'match_status': 'verified', 'match_basis': 'doi',
           'blocks': [{'type': 'paragraph', 'text': 'Allowed'}, {'type': 'image', 'src': src}]}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {'state': 'matched', 'articles': [row]}}})
    path = tmp_path / 'site' / src
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\x89PNG\r\n\x1a\nnot the hashed image')
    assert coverage.publish_assets(tmp_path, tmp_path / 'site', NOW) == set()
    assert not path.exists()


def test_all_returned_articles_are_read_beyond_twenty_without_deferral(tmp_path):
    papers = [{**PAPER, 'id': f'{i:012x}', 'doi': f'10.1234/{i}', 'title': PAPER['title'] + f' variant {i}'} for i in range(4)]
    setup(tmp_path, papers)
    searches, reads = [], []
    def many_results(query, key):
        searches.append(query)
        return {'organic_results': [{'link': f'https://mp.weixin.qq.com/s/result-{len(searches)}-{i}', 'title': '候选文章'} for i in range(5)]}
    def unrelated(url, source):
        reads.append(url)
        return page('与当前论文无关的其他研究内容。' * 10)
    result = coverage.collect(tmp_path, NOW, key='key', searcher=many_results, fetcher=unrelated)
    assert len(searches) == sum(len(coverage._queries(p)) for p in papers) and len(reads) == 5 * len(searches)
    third = result['papers'][papers[2]['id']]
    assert third['state'] == 'pending_match'
    assert {c['reason'] for c in third['candidates']} == {'identity_unconfirmed'}


def test_deadline_preserves_each_completed_match_and_unprocessed_history(tmp_path):
    papers = [{**PAPER, 'id': f'{i:012x}', 'doi': f'10.1234/{i}', 'title': PAPER['title'] + f' variant {i}'} for i in range(4)]
    setup(tmp_path, papers)
    prior = {'url': URL, 'match_status': 'verified', 'match_basis': 'doi', 'account': '历史来源'}
    write(tmp_path / coverage.DATA, {'papers': {papers[3]['id']: {'state': 'matched', 'checked_at': NOW.isoformat(), 'articles': [prior]}}})
    ticks, reads = [0], []
    def discover(query, key):
        number = len(reads)
        return {'organic_results': [{'link': f'https://mp.weixin.qq.com/s/paper-{number}', 'title': '研究'}]}
    def fetched(url, source):
        number = len(reads)
        if number:
            saved = read(tmp_path / coverage.DATA)['papers']
            assert saved[papers[0]['id']]['state'] == 'matched'
        reads.append(url)
        ticks[0] += 60
        return page('研究的详细介绍。' * 20 + f' DOI: 10.1234/{number}')
    result = coverage.collect(tmp_path, NOW, key='key', searcher=discover, fetcher=fetched,
                              max_seconds=100, monotonic=lambda: ticks[0])
    assert len(reads) == 2
    assert [result['papers'][p['id']]['state'] for p in papers] == ['matched', 'matched', 'deferred', 'matched']
    assert result == read(tmp_path / coverage.DATA)


def test_confirmed_match_checkpoint_survives_interrupted_summary(tmp_path):
    setup(tmp_path)
    def interrupted(*_):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=lambda *_: page(), summarizer=interrupted)
    saved = read(tmp_path / coverage.DATA)['papers'][PAPER['id']]
    assert saved['state'] == 'matched' and saved['articles'][0]['summary_status'] == 'pending'


@pytest.mark.parametrize('max_seconds, expected', [(None, 30), (360, 30), (25, 3)])
def test_reprint_has_no_image_quota_but_honors_explicit_deadline(tmp_path, max_seconds, expected):
    setup(tmp_path)
    grant(tmp_path, scope=['body', 'images'])
    out = io.BytesIO()
    Image.new('RGB', (3, 2), 'red').save(out, format='PNG')
    images = ''.join(f'<img data-src="https://mmbiz.qpic.cn/mmbiz_png/{i}/640">' for i in range(30))
    body = page().replace(b'</div>', images.encode() + b'</div>')
    ticks, reads = [0], []
    def fetched(url, source):
        if url == URL:
            return body
        reads.append(url)
        ticks[0] += 10
        return out.getvalue()
    result = coverage.collect(tmp_path, NOW, key='key', searcher=search, fetcher=fetched,
                              max_seconds=max_seconds, monotonic=lambda: ticks[0])
    article = result['papers'][PAPER['id']]['articles'][0]
    assert len(reads) == expected
    assert article['reprint_complete'] is (expected == 30)
    if expected < 30:
        assert f'{expected}/30' in article['reprint_notice']
    assert sum(b['type'] == 'image' for b in article['blocks']) == expected


def test_search_reads_the_twelfth_candidate_and_deduplicates_urls(tmp_path):
    setup(tmp_path)
    urls = [f'https://mp.weixin.qq.com/s/result-{i}' for i in range(12)]
    reads = []
    def discover(*_):
        return {'organic_results': [{'link': url, 'title': '候选'} for url in urls + urls]}
    def fetched(url, _):
        reads.append(url)
        return page() if url == urls[-1] else page('另一篇研究的详细介绍。' * 20)
    row = coverage.collect(tmp_path, NOW, key='key', searcher=discover, fetcher=fetched)['papers'][PAPER['id']]
    assert sorted(reads) == sorted(urls) and row['state'] == 'matched'
    assert [a['url'] for a in row['articles']] == [urls[-1]]


def test_local_archive_is_not_truncated_before_matching_source(tmp_path):
    setup(tmp_path)
    urls = [f'https://mp.weixin.qq.com/s/local-{i}' for i in range(12)]
    write(tmp_path / 'data/social-articles.json', {'entries': [
        {'url': url, 'title': PAPER['title']} for url in urls]})
    reads = []
    def fetched(url, _):
        reads.append(url)
        return page() if url == urls[-1] else page('另一篇研究的详细介绍。' * 20)
    row = coverage.collect(tmp_path, NOW, key='', fetcher=fetched)['papers'][PAPER['id']]
    assert sorted(reads) == sorted(urls) and row['state'] == 'matched'
    assert row['articles'][0]['url'] == urls[-1]


def test_all_matched_papers_receive_summaries_without_six_item_quota(tmp_path):
    papers = [{**PAPER, 'id': f'{i:012x}', 'doi': f'10.1234/{i}', 'title': PAPER['title'] + f' variant {i}'} for i in range(8)]
    setup(tmp_path, papers)
    calls = []
    def discover(query, _):
        number = query.split('10.1234/')[1].split('"')[0]
        return {'organic_results': [{'link': f'https://mp.weixin.qq.com/s/{number}'}]}
    def fetched(url, _):
        return page('研究团队介绍了风机数字孪生的新研究方法和实验结果。' * 4 + ' DOI: 10.1234/' + url.rsplit('/', 1)[1])
    def summarize(article, paper):
        calls.append(paper['id'])
        return {'summary': '公众号讨论了一项面向风电机组的建模工作：把机理知识引入数字化模型，用于设备故障预测，并结合实验呈现方法效果。具体技术细节与实验条件仍需查阅论文。',
                'evidence': ['研究团队介绍了风机数字孪生的新研究方法和实验结果。']}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=discover, fetcher=fetched, summarizer=summarize)
    assert calls == [p['id'] for p in papers]
    assert all(row['articles'][0]['summary_status'] == 'ready' for row in result['papers'].values())


def test_default_has_no_runtime_cutoff_and_reports_cached_or_completed_papers(tmp_path):
    papers = [{**PAPER, 'id': f'{i:012x}', 'doi': f'10.1234/{i}', 'title': PAPER['title'] + f' variant {i}'} for i in range(4)]
    setup(tmp_path, papers)
    ticks, calls, events = [0], [], []
    def empty(query, _):
        calls.append(query)
        ticks[0] += 500
        return {'organic_results': []}
    def progress(done, total, paper, record):
        events.append((done, total, paper['id'], record['state']))
        assert read(tmp_path / coverage.DATA)['papers'][paper['id']] == record
    result = coverage.collect(tmp_path, NOW, key='key', searcher=empty, monotonic=lambda: ticks[0], progress_callback=progress)
    assert ticks[0] == 500 * sum(len(coverage._queries(p)) for p in papers) and len(calls) == sum(len(coverage._queries(p)) for p in papers)
    assert events == [(i + 1, 4, p['id'], 'no_match') for i, p in enumerate(papers)]
    events.clear()
    again = coverage.collect(tmp_path, NOW + timedelta(minutes=1), key='key', searcher=empty, progress_callback=progress)
    assert len(calls) == sum(len(coverage._queries(p)) for p in papers) and len(events) == 4
    assert again['papers'] == result['papers']


def test_refresh_bypasses_search_cache_and_targeted_run_preserves_other_records(tmp_path):
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other', 'title': PAPER['title'] + ' other'}
    setup(tmp_path, [PAPER, other])
    calls = []
    empty = lambda query, _: calls.append(query) or {'organic_results': []}
    first = coverage.collect(tmp_path, NOW, key='key', searcher=empty)
    assert len(calls) == len(coverage._queries(PAPER)) + len(coverage._queries(other))
    later = NOW + timedelta(minutes=5)
    result = coverage.collect(tmp_path, later, key='key', searcher=empty, force_refresh=True, paper_ids=[PAPER['id']])
    assert len(calls) == 2 * len(coverage._queries(PAPER)) + len(coverage._queries(other)) and result['papers'][PAPER['id']]['checked_at'] == later.isoformat()
    assert result['papers'][other['id']] == first['papers'][other['id']]
    reads = []
    fetched = lambda url, _: reads.append(url) or page()
    coverage._cached_article(tmp_path, URL, NOW, fetched)
    coverage._cached_article(tmp_path, URL, later, fetched)
    coverage._cached_article(tmp_path, URL, later, fetched, force_refresh=True)
    assert reads == [URL, URL]


def test_finite_query_strategy_normalizes_title_punctuation_and_relaxes_phrase_only(tmp_path):
    paper = {**PAPER, 'title': 'Physics–informed digital twins for wind turbine fault prognosis'}
    queries = coverage._queries(paper)
    assert queries[:4] == ['site:mp.weixin.qq.com "10.1234/fixture"',
                       'site:mp.weixin.qq.com "Physics–informed digital twins for wind turbine fault prognosis"',
                       'site:mp.weixin.qq.com "Physics-informed digital twins for wind turbine fault prognosis"',
                       'site:mp.weixin.qq.com Physics-informed digital twins for wind turbine fault prognosis']
    setup(tmp_path, [paper])
    calls = []
    def discover(query, _):
        calls.append(query)
        return search(query, '') if query == queries[-1] else {'organic_results': []}
    row = coverage.collect(tmp_path, NOW, key='key', searcher=discover, fetcher=lambda *_: page())['papers'][paper['id']]
    assert calls == queries and row['state'] == 'matched'
    assert coverage.match_article(paper, coverage.parse_article(page('另一篇相关研究内容。' * 20), URL)) == ''
