from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError

import pytest

from src.coverage_links import HEALTH, PRIVATE, mark_refreshed, remember, resume
from src.public_sources import read, write
from src.sources.wechat_public_index import PublicSearchChallenge
from src.wechat_metadata import public_index_url


NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
PAPER = {'id': 'paper-123'}
ARTICLE = 'https://mp.weixin.qq.com/s/DXvEHDH1FQRqGQkQhBYKyw'


def candidate(token='PRIVATE', title='晶体生成模型研究'):
    return {'title': title, 'account': 'Ai赋能材料', 'published_at': '2026-07-22T18:00:00+08:00',
            '_redirect_url': 'https://weixin.sogou.com/link?url=' + token}


def tasks(root, paper=PAPER):
    return [read(path) for path in (root / PRIVATE / paper['id']).glob('*.json')]


def test_recovers_direct_link_persistently_without_repeating_io_or_disclosing_redirect(tmp_path):
    source = candidate()
    remember(tmp_path, PAPER, source, NOW)
    calls = []
    def resolve(url):
        calls.append(url)
        return ARTICLE + '?tracking=private'
    result = resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert len(calls) == 1 and result['attempted_at'] == NOW.isoformat()
    assert result['candidates'] == [{'url': ARTICLE, 'title': source['title'], 'account': source['account'],
                                    'published_at': source['published_at'], 'reason': 'identity_unconfirmed',
                                    'index_url': public_index_url(source['account'], source['title'])}]
    assert 'PRIVATE' not in str(result)
    remember(tmp_path, PAPER, source, NOW + timedelta(days=2))
    repeated = resume(tmp_path, PAPER, NOW + timedelta(days=2), resolver=resolve)
    assert len(calls) == 1 and repeated['attempted_at'] == ''
    assert repeated['candidates'] == result['candidates']


@pytest.mark.parametrize('url', [
    'https://evil.example/link?url=x', 'https://weixin.sogou.com.evil.example/link?url=x',
    'https://user:password@weixin.sogou.com/link?url=x', 'https://weixin.sogou.com:4430/link?url=x',
    'file:///link?url=x', 'https://weixin.sogou.com/weixin?query=x',
    'https://weixin.sogou.com/link', 'https://weixin.sogou.com/link?url=x\nmalicious',
    'https://weixin.sogou.com/link?url=x#fragment',
])
def test_rejects_unapproved_redirect_inputs(tmp_path, url):
    assert remember(tmp_path, PAPER, {**candidate(), '_redirect_url': url}, NOW) is None
    assert tasks(tmp_path) == []


def test_relative_redirect_and_paper_isolation(tmp_path):
    remember(tmp_path, PAPER, {**candidate(), '_redirect_url': '/link?url=PRIVATE'}, NOW)
    assert tasks(tmp_path)[0]['redirect_url'] == 'https://weixin.sogou.com/link?url=PRIVATE'
    assert resume(tmp_path, {'id': 'other-paper'}, NOW, resolver=lambda _: pytest.fail('wrong paper'))['candidates'] == []
    with pytest.raises(ValueError):
        remember(tmp_path, {'id': '../escape'}, candidate(), NOW)


def test_redirect_captcha_pauses_channel_but_keeps_independent_index_health(tmp_path):
    for number in range(5):
        remember(tmp_path, PAPER, candidate(str(number), '研究' + str(number)), NOW)
    index_health = tmp_path / 'data/cache/paper-coverage/providers/sogou.json'
    write(index_health, {'scope': 'index', 'state': 'ok'})
    calls = []
    def resolve(url):
        calls.append(url)
        error = PublicSearchChallenge('captcha')
        error.headers = {'Retry-After': '600'}
        raise error
    result = resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert len(calls) == 1 and result['attempted_at'] == NOW.isoformat()
    assert read(index_health) == {'scope': 'index', 'state': 'ok'}
    health = read(tmp_path / HEALTH)
    assert health['scope'] == 'redirect' and health['state'] == 'blocked'
    assert health['retry_at'] == (NOW + timedelta(minutes=10)).isoformat()
    assert resume(tmp_path, PAPER, NOW + timedelta(minutes=5), resolver=resolve)['attempted_at'] == ''
    assert len(calls) == 1


@pytest.mark.parametrize('headers,delay', [({}, timedelta(days=1)),
                                         ({'Retry-After': 'Thu, 15 Oct 2026 00:20:00 GMT'}, timedelta(minutes=20))])
def test_http_restriction_uses_retry_hint_or_next_day(tmp_path, headers, delay):
    remember(tmp_path, PAPER, candidate(), NOW)
    def resolve(url):
        raise HTTPError(url, 429, 'limited', headers, None)
    resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert read(tmp_path / HEALTH)['retry_at'] == (NOW + delay).isoformat()


def test_new_href_and_evidence_processed_immediately_except_real_service_backoff(tmp_path):
    source = candidate()
    remember(tmp_path, PAPER, source, NOW)
    def fails(_):
        raise TimeoutError()
    resume(tmp_path, PAPER, NOW, resolver=fails)
    assert tasks(tmp_path)[0]['state'] == 'error'
    remember(tmp_path, PAPER, candidate('NEW'), NOW + timedelta(minutes=1))
    assert resume(tmp_path, PAPER, NOW + timedelta(minutes=1), resolver=lambda _: ARTICLE)['candidates']
    remember(tmp_path, PAPER, {**candidate('NEW'), 'source_version': 'new-evidence'}, NOW + timedelta(minutes=2))
    assert tasks(tmp_path)[0]['state'] == 'pending'
    write(tmp_path / HEALTH, {'scope': 'redirect', 'state': 'blocked', 'retry_at': (NOW + timedelta(hours=1)).isoformat()})
    result = resume(tmp_path, PAPER, NOW + timedelta(minutes=2), resolver=lambda _: pytest.fail('backoff'))
    assert result['attempted_at'] == '' and result['candidates'] == []
    assert resume(tmp_path, PAPER, NOW + timedelta(hours=1), resolver=lambda _: ARTICLE)['candidates']
    assert read(tmp_path / HEALTH)['state'] == 'ok'


@pytest.mark.parametrize('result', ['', 'https://evil.example/article'])
def test_missing_or_non_article_redirect_requests_fresh_title_search(tmp_path, result):
    source = candidate()
    remember(tmp_path, PAPER, source, NOW)
    first = resume(tmp_path, PAPER, NOW, resolver=lambda _: result)
    assert first['candidates'] == []
    assert first['refresh_queries'] == ['site:mp.weixin.qq.com "Ai赋能材料" "晶体生成模型研究"']
    again = resume(tmp_path, PAPER, NOW + timedelta(minutes=1), resolver=lambda _: pytest.fail('expired href'))
    assert again['refresh_queries'] == first['refresh_queries'] and again['attempted_at'] == ''
    remember(tmp_path, PAPER, candidate('REFRESHED'), NOW + timedelta(minutes=1))
    assert resume(tmp_path, PAPER, NOW + timedelta(minutes=1), resolver=lambda _: ARTICLE)['candidates']


@pytest.mark.parametrize('code', [404, 410])
def test_expired_http_redirect_requests_fresh_title_search(tmp_path, code):
    remember(tmp_path, PAPER, candidate(), NOW)
    def resolve(url):
        raise HTTPError(url, code, 'expired', {}, None)
    result = resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert result['refresh_queries'] and not result['candidates']
    assert not (tmp_path / HEALTH).exists()


def test_temporary_errors_retry_next_day_without_erasing_tasks(tmp_path):
    remember(tmp_path, PAPER, candidate(), NOW)
    calls = []
    def resolve(url):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError()
        return ARTICLE
    resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert resume(tmp_path, PAPER, NOW + timedelta(hours=1), resolver=resolve)['attempted_at'] == ''
    assert resume(tmp_path, PAPER, NOW + timedelta(days=1), resolver=resolve)['candidates']
    assert len(calls) == 2


def test_no_daily_or_per_paper_count_cutoff(tmp_path):
    for index in range(14):
        remember(tmp_path, PAPER, candidate(str(index), '研究' + str(index)), NOW)
    calls = []
    def resolve(url):
        calls.append(url)
        return ARTICLE + '_' + str(len(calls))
    result = resume(tmp_path, PAPER, NOW, resolver=resolve)
    assert len(calls) == 14 and len(result['candidates']) == 14


def test_resolved_links_remain_available_during_channel_backoff(tmp_path):
    remember(tmp_path, PAPER, candidate(), NOW)
    resume(tmp_path, PAPER, NOW, resolver=lambda _: ARTICLE)
    write(tmp_path / HEALTH, {'scope': 'redirect', 'state': 'blocked', 'retry_at': (NOW + timedelta(days=1)).isoformat()})
    result = resume(tmp_path, PAPER, NOW, resolver=lambda _: pytest.fail('completed'))
    assert result['candidates'][0]['url'] == ARTICLE and result['attempted_at'] == ''


def test_default_resolver_uses_normal_public_redirect_transport(tmp_path, monkeypatch):
    from src import coverage_discovery
    remember(tmp_path, PAPER, candidate(), NOW)
    calls = []
    def transport(url):
        calls.append(url)
        return {'body': b'', 'url': ARTICLE, 'status': 200}
    monkeypatch.setattr(coverage_discovery, 'request_page', transport)
    assert resume(tmp_path, PAPER, NOW)['candidates'][0]['url'] == ARTICLE
    assert calls == [candidate()['_redirect_url']]


def test_execution_deadline_defers_io_without_changing_pending_task(tmp_path):
    for index in range(3):
        remember(tmp_path, PAPER, candidate(str(index), '研究' + str(index)), NOW)
    before = tasks(tmp_path)
    callbacks = []
    def before_request():
        callbacks.append(True)
        return False
    result = resume(tmp_path, PAPER, NOW, before_request=before_request,
                    resolver=lambda _: pytest.fail('execution deadline reached'))
    assert len(callbacks) == 1 and result['attempted_at'] == ''
    assert tasks(tmp_path) == before
    resumed = resume(tmp_path, PAPER, NOW, before_request=lambda: True, resolver=lambda _: ARTICLE)
    assert resumed['candidates'][0]['url'] == ARTICLE


def test_deadline_callback_only_precedes_actual_resolver_calls(tmp_path):
    remember(tmp_path, PAPER, candidate(), NOW)
    resume(tmp_path, PAPER, NOW, resolver=lambda _: ARTICLE)
    result = resume(tmp_path, PAPER, NOW, before_request=lambda: pytest.fail('cached result'),
                    resolver=lambda _: pytest.fail('cached result'))
    assert result['candidates'] and result['attempted_at'] == ''


def test_completed_refresh_search_waits_one_day_but_unhandled_query_remains_due(tmp_path):
    remember(tmp_path, PAPER, candidate(), NOW)
    result = resume(tmp_path, PAPER, NOW, resolver=lambda _: '')
    queries = result['refresh_queries']
    assert queries
    assert mark_refreshed(tmp_path, PAPER, ['different query'], NOW) == 0
    assert resume(tmp_path, PAPER, NOW)['refresh_queries'] == queries
    assert mark_refreshed(tmp_path, PAPER, queries, NOW) == 1
    task = tasks(tmp_path)[0]
    assert task['refresh_checked_at'] == NOW.isoformat()
    assert task['next_refresh_at'] == (NOW + timedelta(days=1)).isoformat()
    assert resume(tmp_path, PAPER, NOW + timedelta(hours=23))['refresh_queries'] == []
    assert resume(tmp_path, PAPER, NOW + timedelta(days=1))['refresh_queries'] == queries


def test_new_href_clears_refresh_cooldown_and_resolves_immediately(tmp_path):
    remember(tmp_path, PAPER, candidate(), NOW)
    result = resume(tmp_path, PAPER, NOW, resolver=lambda _: '')
    mark_refreshed(tmp_path, PAPER, result['refresh_queries'], NOW)
    remember(tmp_path, PAPER, candidate(), NOW + timedelta(minutes=1))
    assert resume(tmp_path, PAPER, NOW + timedelta(minutes=1))['refresh_queries'] == []
    remember(tmp_path, PAPER, candidate('NEW'), NOW + timedelta(minutes=2))
    assert 'next_refresh_at' not in tasks(tmp_path)[0]
    assert resume(tmp_path, PAPER, NOW + timedelta(minutes=2), resolver=lambda _: ARTICLE)['candidates']
