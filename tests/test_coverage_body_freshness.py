"""An unrelated newer paper attempt cannot make an old source cache current."""
from datetime import timedelta

import pytest

from src import paper_coverage as coverage
from src.public_sources import write
from tests.test_paper_coverage import NOW, PAPER, URL, setup, page, grant
from tests.test_merge_coverage_results import article, content_article, record, snapshot
from tools.merge_coverage_results import export_results, merge_results


EARLY = NOW - timedelta(hours=3)
CURRENT = NOW
LATER = NOW + timedelta(hours=1)


@pytest.mark.parametrize('cached_body', [b'<html>captcha</html>', page()])
@pytest.mark.parametrize('legacy', [False, True])
def test_old_cached_failure_or_content_cannot_overwrite_later_browser_read(tmp_path, cached_body, legacy):
    setup(tmp_path)
    grant(tmp_path)
    coverage._cached_article(tmp_path, URL, EARLY, lambda *_: cached_body)
    prior = content_article('b', '浏览器已核验新正文')
    if not legacy:
        prior['body_checked_at'] = CURRENT.isoformat()
    other_url = URL + '-unrelated-new-read'
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: record([prior], candidates=[
        {'url': other_url, 'title': PAPER['title'], 'reason': 'identity_unconfirmed'}])}})
    calls = []
    def fetch(url, *_):
        calls.append(url)
        return page()
    result = coverage.collect(tmp_path, LATER, perform_search=False, fetcher=fetch, summarizer=lambda *_: None)
    row = next(a for a in result['papers'][PAPER['id']]['articles'] if a['url'] == URL)
    assert calls == [other_url]
    assert result['papers'][PAPER['id']]['last_attempt_at'] == LATER.isoformat()
    assert row['body_status'] == 'readable' and row['source_version'] == 'b' * 64
    assert row['summary'] == prior['summary'] and row['blocks'] == prior['blocks']
    if not legacy:
        assert row['body_checked_at'] == CURRENT.isoformat()
    else:
        assert 'body_checked_at' not in row  # Do not invent a fetch time from verification.


def test_real_later_captcha_updates_observation_but_keeps_identity_and_saved_content(tmp_path):
    setup(tmp_path)
    grant(tmp_path)
    prior = {**content_article('b', '已读取报道'), 'body_checked_at': CURRENT.isoformat()}
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: record([prior])}})
    result = coverage.collect(tmp_path, LATER, perform_search=False, force_refresh=True,
        fetcher=lambda *_: b'<html>captcha</html>', summarizer=lambda *_: None)
    row, = result['papers'][PAPER['id']]['articles']
    assert row['body_checked_at'] == LATER.isoformat() and row['body_status'] == 'challenge'
    assert row['match_status'] == 'verified' and row['source_version'] == 'b' * 64
    assert row['summary'] == prior['summary'] and row['blocks'] == prior['blocks']


def test_success_cache_keeps_actual_source_time_instead_of_collection_time(tmp_path):
    setup(tmp_path)
    coverage._cached_article(tmp_path, URL, EARLY, lambda *_: page())
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: record([], candidates=[
        {'url': URL, 'title': PAPER['title'], 'reason': 'identity_unconfirmed'}])}})
    result = coverage.collect(tmp_path, LATER, perform_search=False,
        fetcher=lambda *_: pytest.fail('Must use saved readable cache'), summarizer=lambda *_: None)
    row, = result['papers'][PAPER['id']]['articles']
    assert row['body_checked_at'] == EARLY.isoformat()
    assert row['verified_at'] == LATER.isoformat()


def test_old_mirror_failure_does_not_override_newer_readable_mirror(tmp_path):
    setup(tmp_path)
    mirror_url = 'https://hub.baai.ac.cn/view/53949'
    prior = article(body_checked_at=CURRENT.isoformat(), mirrors=[{
        'url': mirror_url, 'body_status': 'readable', 'kind': 'article',
        'checked_at': CURRENT.isoformat()}])
    write(tmp_path / coverage.DATA, {'papers': {PAPER['id']: record([prior])}})
    for kind, url in [('articles', URL), ('syndication', mirror_url)]:
        write(tmp_path / coverage.CACHE / kind / (coverage._identity(url) + '.json'), {
            'version': coverage.ARTICLE_VERSION, 'state': 'challenge', 'checked_at': EARLY.isoformat()})
    result = coverage.collect(tmp_path, LATER, perform_search=False,
        fetcher=lambda *_: pytest.fail('Expected cache reads'), summarizer=lambda *_: None)
    row, = result['papers'][PAPER['id']]['articles']
    assert row['mirrors'] == prior['mirrors'] and row['body_status'] == 'readable'


@pytest.mark.parametrize('kind', ['copy', 'official'])
def test_reviewed_sources_project_actual_observation_and_ignore_old_cache(tmp_path, kind):
    if kind == 'copy':
        from tests.test_coverage_account_copy import approve, COPY
        target_paper, url = PAPER, COPY['url']
        receipt = approve(tmp_path)
        cache_kind = 'syndication'
    else:
        from tests.test_coverage_official_crosslink import approve, PAPER as target_paper, URL as url
        receipt = approve(tmp_path)
        cache_kind = 'articles'
    setup(tmp_path, [target_paper])
    prior = {**receipt['article'], 'body_status': 'readable', 'body_checked_at': CURRENT.isoformat(), 'mirrors': []}
    write(tmp_path / coverage.DATA, {'papers': {target_paper['id']: record([prior])}})
    write(tmp_path / coverage.CACHE / cache_kind / (coverage._identity(url) + '.json'), {
        'version': coverage.ARTICLE_VERSION, 'state': 'challenge', 'checked_at': EARLY.isoformat()})
    result = coverage.collect(tmp_path, LATER, perform_search=False,
        fetcher=lambda *_: b'<html>captcha</html>', summarizer=lambda *_: None)
    row, = result['papers'][target_paper['id']]['articles']
    assert row['body_status'] == 'readable' and row['body_checked_at'] == CURRENT.isoformat()
    # Actual forced failure is later and must remain public through the receipt gate.
    result = coverage.collect(tmp_path, LATER, perform_search=False, force_refresh=True,
        fetcher=lambda *_: b'<html>captcha</html>', summarizer=lambda *_: None)
    row, = result['papers'][target_paper['id']]['articles']
    assert row['body_status'] == 'challenge' and row['body_checked_at'] == LATER.isoformat()


def test_new_record_with_old_source_failure_cannot_override_newer_body_in_merge(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'artifact'
    setup(current)
    grant(current)
    prior = {**content_article('b', '已核验新正文'), 'body_checked_at': CURRENT.isoformat()}
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([prior])}})
    snapshot(incoming, {PAPER['id']: record([article(body_status='challenge',
        body_checked_at=EARLY.isoformat())], LATER)})
    merge_results(incoming, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['body_status'] == 'readable' and row['body_checked_at'] == CURRENT.isoformat()
    assert row['summary'] == prior['summary'] and row['blocks'] == prior['blocks']


def test_actual_later_failure_wins_even_when_other_record_attempt_is_newer(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'artifact'
    setup(current)
    grant(current)
    prior = {**content_article('b', '已核验新正文'), 'body_checked_at': CURRENT.isoformat()}
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([prior], LATER + timedelta(hours=2))}})
    snapshot(incoming, {PAPER['id']: record([article(body_status='challenge',
        verified_at=EARLY.isoformat(), body_checked_at=LATER.isoformat())], LATER)})
    merge_results(incoming, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['body_status'] == 'challenge' and row['body_checked_at'] == LATER.isoformat()
    assert row['source_version'] == 'b' * 64 and row['blocks'] == prior['blocks']


@pytest.mark.parametrize('kind', ['copy', 'official'])
def test_reviewed_observation_time_survives_export_and_import(tmp_path, kind):
    if kind == 'copy':
        from tests.test_coverage_account_copy import approve
        target_paper = PAPER
    else:
        from tests.test_coverage_official_crosslink import approve, PAPER as target_paper
    collected, outgoing, current = tmp_path / 'collector', tmp_path / 'artifact', tmp_path / 'checkout'
    setup(collected, [target_paper]); setup(current, [target_paper])
    receipt = approve(collected)
    write(collected / coverage.DATA, {'papers': {target_paper['id']: record([
        {**receipt['article'], 'body_status': 'unavailable', 'body_checked_at': CURRENT.isoformat()}])}})
    export_results(outgoing, collected)
    merge_results(outgoing, current)
    row, = coverage.load_public(current)[target_paper['id']]['articles']
    assert row['body_checked_at'] == CURRENT.isoformat() and row['body_status'] == 'unavailable'
