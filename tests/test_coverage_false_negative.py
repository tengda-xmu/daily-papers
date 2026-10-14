"""Known-positive recall and failure recovery, not just an empty queue."""
from datetime import timedelta
import json

import pytest

from src import paper_coverage as coverage
from src.coverage_evidence import PRIVATE, PUBLIC, save_manual_review
from src.public_sources import read, write
from tests.test_paper_coverage import NOW, PAPER, URL, IMAGE, page, setup

KNOWN_URL = 'https://mp.weixin.qq.com/s/DXvEHDH1FQRqGQkQhBYKyw'
KNOWN = {'id': 'c53c3b080d69', 'doi': '10.1038/s42256-026-01262-4',
         'title': 'Guiding generative models to uncover diverse and novel crystals via reinforcement learning',
         'title_zh': '通过强化学习引导生成模型发现多样且新颖的晶体',
         'authors': ['Hyunsoo Park', 'Aron Walsh'], 'summary': '提出Chemeleon2晶体生成模型。'}
HEADLINE = 'npj Comput. Mater. | 晶体生成模型的新颖稳定性能协同优化：基于强化学习的未知材料主动探索研究'


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.setattr(coverage, 'model_summary', lambda *_: None)


def manual(root):
    return save_manual_review(root, KNOWN,
        {'url': KNOWN_URL, 'title': HEADLINE, 'account': 'Ai赋能材料',
         'published_at': '2026-07-22T18:00:00+08:00', 'body_status': 'challenge'},
        {'dois': [KNOWN['doi']], 'title': KNOWN['title'], 'authors': KNOWN['authors'],
         'legible': True, 'context': 'paper_front_page', 'main_topic': True,
         'reference_only': False, 'conflict': False,
         'provenance': 'User screenshot and separate source URL, conversation 2026-10-14'})


def test_known_source_survives_captcha_and_wrong_headline_journal(tmp_path):
    setup(tmp_path, [KNOWN])
    write(tmp_path / coverage.DATA, {'version': 1, 'papers': {KNOWN['id']: {
        'state': 'no_match', 'checked_at': NOW.isoformat(), 'articles': [], 'candidates': []}}})
    receipt = manual(tmp_path)
    reads = []
    result = coverage.collect(tmp_path, NOW, key='', paper_ids=[KNOWN['id']], source_urls=[KNOWN_URL],
        fetcher=lambda url, _: reads.append(url) or b'<html>wappoc_appmsgcaptcha</html>')
    row = result['papers'][KNOWN['id']]
    assert reads == [KNOWN_URL]
    assert row['state'] == 'matched' and not row['candidates']
    article = row['articles'][0]
    assert article['url'] == KNOWN_URL and article['title'] == HEADLINE
    assert article['account'] == 'Ai赋能材料' and article['published_at'].startswith('2026-07-22')
    assert article['match_basis'] == 'manual_screenshot' and article['body_status'] == 'challenge'
    assert article['summary_status'] == 'pending' and article['display_mode'] == 'source'
    assert article['summary'] == ''
    assert 'blocks' not in article and 'last_search_at' not in row
    public = json.dumps(result, ensure_ascii=False)
    assert 'provenance' not in public and 'transcription' not in public
    assert (tmp_path / PRIVATE / 'reviews' / (receipt['id'] + '.json')).exists()
    assert (tmp_path / PUBLIC / (receipt['id'] + '.json')).exists()
    again = coverage.collect(tmp_path, NOW + timedelta(days=2), key='', fetcher=lambda *_: b'<html>captcha</html>')
    assert again['papers'][KNOWN['id']]['articles'][0]['url'] == KNOWN_URL


def test_hint_reads_immediately_but_url_alone_cannot_verify(tmp_path):
    setup(tmp_path)
    coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': []})
    reads = []
    result = coverage.collect(tmp_path, NOW + timedelta(minutes=1), key='', paper_ids=[PAPER['id']],
        source_urls=[URL], fetcher=lambda url, _: reads.append(url) or page('这是另一篇论文的介绍。' * 20))
    assert reads == [URL]
    row = result['papers'][PAPER['id']]
    assert not row['articles'] and row['candidates'][0]['url'] == URL


def test_hint_bypasses_article_failure_cache_as_well_as_paper_cache(tmp_path):
    setup(tmp_path)
    coverage._cached_article(tmp_path, URL, NOW, lambda *_: b'<html>captcha</html>')
    reads = []
    row = coverage.collect(tmp_path, NOW, key='', paper_ids=[PAPER['id']], source_urls=[URL],
        fetcher=lambda url, _: reads.append(url) or page())['papers'][PAPER['id']]
    assert reads == [URL] and row['state'] == 'matched'


@pytest.mark.parametrize('ids,urls', [(None, [URL]), ([], [URL]), ([PAPER['id'], 'second'], [URL]),
                                     ([PAPER['id']], ['https://example.com/source'])])
def test_hint_rejects_ambiguous_or_unsafe_input_before_writing(tmp_path, ids, urls):
    setup(tmp_path)
    with pytest.raises(ValueError):
        coverage.collect(tmp_path, NOW, key='', paper_ids=ids, source_urls=urls)
    assert not (tmp_path / coverage.DATA).exists()


def test_image_only_article_enters_visual_queue_not_no_match(tmp_path):
    setup(tmp_path)
    body = ('<h1 id="activity-name">研究介绍</h1><a id="js_name">科研号</a>'
            '<div id="js_content"><p>本研究</p><img data-src="' + IMAGE + '"></div>').encode()
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {
        'organic_results': [{'link': URL}]}, fetcher=lambda *_: body)['papers'][PAPER['id']]
    assert row['state'] == 'pending_match' and not row['articles']
    candidate = row['candidates'][0]
    assert candidate['reason'] == 'visual_pending' and candidate['source_version']
    tasks = list((tmp_path / PRIVATE / 'tasks').glob('*.json'))
    assert len(tasks) == 1
    image = next(b for b in read(tasks[0])['article']['blocks'] if b['type'] == 'image')
    assert image['reference_section'] is False and image['context_before'] == '本研究'
    assert 'remote_url' not in json.dumps(row) and IMAGE not in json.dumps(row)


def test_reference_images_do_not_enter_visual_queue(tmp_path):
    setup(tmp_path)
    body = page('另一篇研究内容。' * 20 + '</p><h2>参考文献</h2><img data-src="' + IMAGE + '"><p>')
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {
        'organic_results': [{'link': URL}]}, fetcher=lambda *_: body)['papers'][PAPER['id']]
    assert row['candidates'][0]['reason'] == 'identity_unconfirmed'
    assert not list((tmp_path / PRIVATE / 'tasks').glob('*.json'))


def test_chinese_archive_candidate_reaches_body_verification(tmp_path):
    setup(tmp_path, [KNOWN])
    write(tmp_path / 'data/social-articles.json', {'entries': [
        {'url': KNOWN_URL, 'title': HEADLINE, 'account': 'Ai赋能材料'}]})
    body = page('本文介绍晶体生成方法。' * 15 + ' DOI: ' + KNOWN['doi'])
    row = coverage.collect(tmp_path, NOW, key='', fetcher=lambda *_: body)['papers'][KNOWN['id']]
    assert row['state'] == 'matched' and row['articles'][0]['match_basis'] == 'doi'


def test_new_strategy_rechecks_legacy_negatives_in_both_caches(tmp_path):
    import hashlib
    setup(tmp_path)
    write(tmp_path / coverage.DATA, {'version': 1, 'papers': {PAPER['id']: {
        'state': 'no_match', 'checked_at': NOW.isoformat()}}})
    for query in coverage._queries(PAPER):
        path = tmp_path / coverage.CACHE / 'searches' / (hashlib.sha256(query.encode()).hexdigest() + '.json')
        write(path, {'state': 'ok', 'rows': [], 'checked_at': NOW.isoformat(), 'provider': 'web_search_tool'})
    calls = []
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda query, _: calls.append(query) or {
        'organic_results': [{'link': URL}]}, fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert calls and row['state'] == 'matched' and row['search_version'] == coverage.SEARCH_VERSION


def test_negative_cache_one_day_positive_seven_days_and_providers_separate(tmp_path):
    calls = []
    def empty(query, _):
        calls.append(query)
        return {'organic_results': []}
    coverage._search(tmp_path, 'empty', NOW, 'key', empty)
    coverage._search(tmp_path, 'empty', NOW + timedelta(hours=23), 'key', empty)
    coverage._search(tmp_path, 'empty', NOW + timedelta(hours=25), 'key', empty)
    assert calls == ['empty', 'empty']
    def positive(query, _):
        calls.append(query)
        return {'organic_results': [{'link': URL}]}
    coverage._search(tmp_path, 'hit', NOW, 'key', positive)
    coverage._search(tmp_path, 'hit', NOW + timedelta(days=6), 'key', positive)
    assert calls.count('hit') == 1
    coverage._search(tmp_path, 'hit', NOW, '', empty, provider='sogou', requires_key=False)
    assert calls.count('hit') == 2


def test_sogou_candidates_survive_partial_block_and_google_empty(tmp_path):
    setup(tmp_path)
    def index(*_):
        return {'state': 'blocked', 'organic_results': [
            {'link': 'https://weixin.sogou.com/weixin?type=2&query=fixture',
             'title': PAPER['title'], 'account': '科研号', 'reason': 'index_only'},
            {'link': URL, 'title': '可读来源', 'snippet': 'DOI: 10.1234/fixture'}]}
    row = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': []},
        index_searcher=index, fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['search_status'] == 'partial'
    assert row['articles'][0]['url'] == URL
    assert row['candidates'][0]['reason'] == 'index_only'


def test_provider_block_does_not_invent_an_attempt_for_other_paper(tmp_path):
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other'}
    setup(tmp_path, [PAPER, other])
    calls = []
    result = coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: calls.append(1) or {
        'state': 'blocked', 'organic_results': [], 'retry_after': 120})
    assert len(calls) == 1
    row = result['papers'][other['id']]
    assert row['search_status'] == 'blocked' and not row.get('last_search_at')
    assert row['last_attempt_at'] == ''
    coverage._search(tmp_path, coverage._queries(PAPER)[0], NOW + timedelta(seconds=121),
                     'key', lambda *_: calls.append(1) or {'organic_results': []})
    assert len(calls) == 2


def test_matched_source_search_failure_retries_next_day(tmp_path):
    setup(tmp_path)
    coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': [{'link': URL}]},
                     fetcher=lambda *_: page())
    calls = []
    def blocked(*_):
        calls.append(1)
        return {'state': 'blocked', 'organic_results': []}
    row = coverage.collect(tmp_path, NOW + timedelta(days=1), key='key', force_refresh=True,
        searcher=blocked, fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert row['state'] == 'matched' and row['last_attempt_state'] == 'unavailable'
    coverage.collect(tmp_path, NOW + timedelta(days=3), key='key', searcher=blocked, fetcher=lambda *_: page())
    assert len(calls) == 2


def test_existing_unconfirmed_candidates_survive_next_search_failure(tmp_path):
    setup(tmp_path)
    coverage.collect(tmp_path, NOW, key='key', searcher=lambda *_: {'organic_results': [{'link': URL}]},
                     fetcher=lambda *_: page('其他论文。' * 40))
    row = coverage.collect(tmp_path, NOW + timedelta(days=2), key='key', searcher=lambda *_: {'error': 'failed'},
        fetcher=lambda *_: page('其他论文。' * 40))['papers'][PAPER['id']]
    assert row['candidates'][0]['url'] == URL and not row['articles']


def test_provider_retry_after_is_honored_by_paper_cache(tmp_path):
    setup(tmp_path)
    calls = []
    def blocked(*_):
        calls.append(1)
        return {'state': 'blocked', 'organic_results': [], 'retry_after': 120}
    coverage.collect(tmp_path, NOW, key='key', searcher=blocked)
    coverage.collect(tmp_path, NOW + timedelta(seconds=60), key='key', searcher=blocked)
    assert len(calls) == 1
    coverage.collect(tmp_path, NOW + timedelta(seconds=121), key='key', searcher=blocked)
    assert len(calls) == 2


def test_results_ignoring_domain_filter_are_failure_not_empty_index(tmp_path):
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other'}
    setup(tmp_path, [PAPER, other])
    calls = []
    def broken_scope(*_):
        calls.append(1)
        return {'search_metadata': {'status': 'Success'}, 'organic_results': [
            {'link': 'https://www.researchgate.net/publication/123', 'title': 'Unrelated research'}]}
    result = coverage.collect(tmp_path, NOW, key='key', searcher=broken_scope)
    assert len(calls) == 4  # DOI differs; both shared title queries are cached.
    assert all(row['state'] == 'error' for row in result['papers'].values())
    assert all(row['search_status'] == 'error' for row in result['papers'].values())
    assert result['papers'][other['id']]['last_search_at'] == NOW.isoformat()
    assert not read(tmp_path / coverage.CACHE / 'providers/serpapi.json')


def test_irrelevant_query_does_not_block_later_title_match(tmp_path):
    setup(tmp_path)
    calls = []
    def search(query, _):
        calls.append(query)
        return {'organic_results': [{'link': 'https://example.org/unrelated'} if len(calls) == 1 else {'link': URL}]}
    row = coverage.collect(tmp_path, NOW, key='key', searcher=search,
                           fetcher=lambda *_: page())['papers'][PAPER['id']]
    assert len(calls) == 2 and row['state'] == 'matched'
    assert row['search_providers']['serpapi']['state'] == 'partial'


def test_legacy_mixed_sogou_health_is_rechecked_without_clearing_true_limits(tmp_path):
    setup(tmp_path)
    future = (NOW + timedelta(days=1)).isoformat()
    providers = tmp_path / coverage.CACHE / 'providers'
    write(providers / 'sogou.json', {'state': 'blocked', 'retry_at': future})
    write(providers / 'serpapi.json', {'state': 'blocked', 'retry_at': future})
    calls = []
    row = coverage.collect(tmp_path, NOW, key='key',
        searcher=lambda *_: pytest.fail('Real API backoff must still be honored'),
        index_searcher=lambda query, _: calls.append(query) or {'organic_results': []})['papers'][PAPER['id']]
    assert len(calls) == len(coverage._queries(PAPER))
    assert row['state'] == 'no_match' and row['search_status'] == 'partial'
    assert row['search_providers']['sogou']['state'] == 'ok'
    assert row['search_providers']['sogou']['queries_completed'] == len(calls)
    assert 'checked_at' not in row['search_providers']['serpapi']
    assert read(providers / 'sogou.json')['scope'] == 'index'
    assert read(providers / 'sogou_redirect.json')['scope'] == 'redirect'


def test_redirect_backoff_does_not_skip_other_queries_or_papers(tmp_path, monkeypatch):
    from src import coverage_discovery
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other'}
    setup(tmp_path, [PAPER, other])
    calls = []
    def index(query, _, *, resolve_links=True):
        calls.append(resolve_links)
        return {'organic_results': [{'link': 'https://weixin.sogou.com/weixin?type=2&query=fixture',
                                     'title': PAPER['title'], 'reason': 'index_only'}],
                'link_resolution': {'state': 'blocked', 'retry_after': 600, 'attempted': resolve_links}}
    monkeypatch.setattr(coverage_discovery, 'sogou_search', index)
    result = coverage.collect(tmp_path, NOW, key='', index_searcher=index)
    assert calls == [True, False, False, False]
    for row in result['papers'].values():
        assert row['state'] == 'pending_match' and row['candidates'] and not row['articles']
        assert row['search_providers']['sogou']['state'] == 'ok'
        assert row['search_providers']['sogou']['link_resolution'] == 'blocked'
    health = read(tmp_path / coverage.CACHE / 'providers/sogou.json')
    assert health['state'] == 'ok' and 'retry_at' not in health


def test_real_index_challenge_still_honors_retry(tmp_path):
    setup(tmp_path)
    write(tmp_path / coverage.CACHE / 'providers/sogou.json', {
        'scope': 'index', 'state': 'blocked', 'retry_at': (NOW + timedelta(hours=1)).isoformat()})
    row = coverage.collect(tmp_path, NOW, key='',
        index_searcher=lambda *_: pytest.fail('Index itself is restricted'))['papers'][PAPER['id']]
    assert row['search_providers']['sogou']['state'] == 'blocked'
    assert not row.get('last_search_at')


def test_blocked_web_channel_does_not_extend_index_negative_cache(tmp_path):
    setup(tmp_path)
    calls = []
    def index(query, _):
        calls.append(query)
        return {'organic_results': []}
    def blocked(*_):
        return {'organic_results': [], 'state': 'blocked', 'retry_after': 7 * 86400}
    coverage.collect(tmp_path, NOW, key='key', searcher=blocked, index_searcher=index)
    coverage.collect(tmp_path, NOW + timedelta(days=2), key='key', searcher=blocked, index_searcher=index)
    assert len(calls) == 2 * len(coverage._queries(PAPER))


def test_redirect_retry_expires_index_cache_without_waiting_seven_days(tmp_path):
    calls = []
    def index(*_):
        calls.append(1)
        return {'organic_results': [{'link': 'https://weixin.sogou.com/weixin?type=2&query=fixture',
                                     'title': PAPER['title']}],
                'link_resolution': {'state': 'blocked', 'attempted': True, 'retry_after': 600}}
    for minutes in (0, 5, 11):
        coverage._search(tmp_path, 'title', NOW + timedelta(minutes=minutes), '', index,
                         provider='sogou', requires_key=False)
    assert len(calls) == 2


def test_provider_timestamp_does_not_go_backwards_on_cache_hit(tmp_path):
    searcher = lambda *_: {'organic_results': []}
    coverage._search(tmp_path, 'old', NOW, 'key', searcher)
    progress = {}
    later = NOW + timedelta(hours=1)
    coverage._search(tmp_path, 'new', later, 'key', searcher, progress=progress)
    coverage._search(tmp_path, 'old', later, 'key', searcher, progress=progress)
    assert progress['providers']['serpapi']['checked_at'] == later.isoformat()


def test_unrelated_index_rows_stay_private_and_snippet_is_not_evidence(tmp_path):
    setup(tmp_path)
    def index(*_):
        return {'organic_results': [
            {'link': 'https://weixin.sogou.com/weixin?type=2&query=finance', 'title': '金融市场与外科人工智能'},
            {'link': URL, 'title': '研究进展', 'snippet': 'Private index extract DOI: 10.1234/fixture'}]}
    row = coverage.collect(tmp_path, NOW, key='', index_searcher=index,
        fetcher=lambda *_: page('其他研究内容。' * 20))['papers'][PAPER['id']]
    assert not row['articles'] and len(row['candidates']) == 1
    assert row['candidates'][0]['url'] == URL
    assert 'Private index extract' not in json.dumps(row)
    assert '"_provider"' not in json.dumps(row)


def test_restored_provider_resumes_papers_it_never_reached(tmp_path):
    other = {**PAPER, 'id': 'bbbbccccdddd', 'doi': '10.1234/other'}
    setup(tmp_path, [PAPER, other])
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: {
        'state': 'no_match', 'search_version': coverage.SEARCH_VERSION,
        'last_search_at': (NOW - timedelta(days=2)).isoformat(),
        'search_providers': {'sogou': {'state': 'ok', 'checked_at': (NOW - timedelta(days=2)).isoformat()}}}}})
    calls = []
    coverage.collect(tmp_path, NOW, key='', index_searcher=lambda query, _: calls.append(query) or {'organic_results': []})
    assert other['doi'] in calls[0]
    assert any(PAPER['doi'] in query for query in calls)
