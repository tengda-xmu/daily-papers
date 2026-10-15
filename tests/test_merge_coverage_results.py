from copy import deepcopy
from datetime import timedelta

import pytest

from src import paper_coverage as coverage
from src.public_sources import read, write
from tests.test_paper_coverage import NOW, PAPER, URL, setup, grant
from tests.test_coverage_account_copy import COPY, approve
from tools.merge_coverage_results import export_results, merge_results


def article(url=URL, **changes):
    return {'url': url, 'title': 'Research report', 'account': 'Research account',
            'match_status': 'verified', 'match_basis': 'doi', 'verification_method': 'body',
            'verified_at': NOW.isoformat(), 'body_status': 'readable', 'source_version': 'a' * 64,
            **changes}


def record(articles=None, when=NOW, **changes):
    return {'state': 'matched' if articles else 'no_match', 'last_attempt_at': when.isoformat(),
            'checked_at': when.isoformat(), 'last_search_at': when.isoformat(), 'search_version': 7,
            'search_status': 'complete', 'articles': articles or [], 'candidates': [], **changes}


def snapshot(root, records):
    write(root / 'paper-coverage.json', {'version': coverage.SCHEMA_VERSION,
          'checked_at': NOW.isoformat(), 'papers': records})


def test_stale_collector_cannot_restore_old_papers_or_replace_newer_coverage(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    new_paper = {**PAPER, 'id': 'newpaper123', 'title': 'New recommendation'}
    setup(current, [PAPER, new_paper])
    daily_before = (current / 'data/daily.json').read_bytes()
    newer = NOW + timedelta(days=1)
    write(current / coverage.DATA, {'version': 3, 'papers': {
        PAPER['id']: record([article(verified_at=newer.isoformat())], newer,
            search_providers={'serpapi': {'state': 'ok', 'checked_at': newer.isoformat(), 'queries_completed': 12}}),
        new_paper['id']: record([article('https://mp.weixin.qq.com/s/new-paper')], newer)}})
    old_url = 'https://mp.weixin.qq.com/s/additional-source'
    snapshot(incoming, {PAPER['id']: record([article(body_status='challenge'), article(old_url)],
        search_version=6, search_status='blocked', candidates=[{'url': 'https://mp.weixin.qq.com/s/old-discarded', 'reason': 'identity_unconfirmed'}],
        search_providers={'serpapi': {'state': 'blocked', 'checked_at': NOW.isoformat(), 'queries_completed': 1}}),
        'deletedpaper': record([article('https://mp.weixin.qq.com/s/deleted')])})
    write(incoming / 'daily.json', {'core': [{'id': 'deletedpaper'}]})
    merge_results(incoming, current)
    result = coverage.load_public(current)
    assert (current / 'data/daily.json').read_bytes() == daily_before
    assert set(result) == {PAPER['id'], new_paper['id']}
    row = result[PAPER['id']]
    assert {a['url'] for a in row['articles']} == {URL, old_url}
    assert next(a for a in row['articles'] if a['url'] == URL)['body_status'] == 'readable'
    assert row['search_version'] == 7 and row['search_status'] == 'complete'
    assert row['search_providers']['serpapi']['queries_completed'] == 12
    assert not row['candidates']


def test_new_failed_scan_retains_current_verified_articles_and_true_query_state(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([article()])}})
    later = NOW + timedelta(hours=1)
    snapshot(incoming, {PAPER['id']: record([], later, state='unavailable', search_status='blocked',
        search_providers={'sogou': {'state': 'blocked', 'checked_at': later.isoformat(), 'queries_completed': 0}})})
    merge_results(incoming, current)
    row = coverage.load_public(current)[PAPER['id']]
    assert row['state'] == 'matched' and row['articles'][0]['url'] == URL
    assert row['search_status'] == 'blocked' and row['last_search_at'] == later.isoformat()
    assert row['search_providers']['sogou']['queries_completed'] == 0


def test_later_source_read_does_not_roll_back_newer_search_strategy(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    current_time = NOW + timedelta(hours=1)
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([], current_time, search_version=8)}})
    snapshot(incoming, {PAPER['id']: record([], NOW + timedelta(hours=2),
        last_search_at=NOW.isoformat(), search_version=7, search_status='blocked')})
    merge_results(incoming, current)
    row = coverage.load_public(current)[PAPER['id']]
    assert row['last_attempt_at'] == (NOW + timedelta(hours=2)).isoformat()
    assert row['last_search_at'] == current_time.isoformat() and row['search_version'] == 8
    assert row['search_status'] == 'complete'


def test_export_strips_raw_private_data_and_merge_is_idempotent(tmp_path):
    collected, outgoing, current = tmp_path / 'collect', tmp_path / 'artifact', tmp_path / 'checkout'
    setup(collected); setup(current)
    write(collected / coverage.DATA, {'version': 3, 'papers': {PAPER['id']: record([
        article(raw_body='PRIVATE', evidence='PRIVATE', blocks=[{'type': 'paragraph', 'text': 'PRIVATE'}])],
        raw_body='PRIVATE', queries=['PRIVATE'])}})
    write(collected / 'data/cache/paper-coverage/private.json', {'transcript': 'PRIVATE'})
    export_results(outgoing, collected)
    exported = read(outgoing / 'paper-coverage.json')
    assert 'PRIVATE' not in str(exported)
    assert {str(path.relative_to(outgoing)).replace('\\', '/') for path in outgoing.rglob('*') if path.is_file()} == {'paper-coverage.json'}
    merge_results(outgoing, current)
    before = (current / coverage.DATA).read_bytes()
    merge_results(outgoing, current)
    assert (current / coverage.DATA).read_bytes() == before


def test_copy_review_survives_public_artifact_and_latest_main_merge(tmp_path):
    collected, outgoing, current = tmp_path / 'collect', tmp_path / 'artifact', tmp_path / 'checkout'
    setup(collected); setup(current)
    receipt = approve(collected)
    write(collected / coverage.DATA, {'version': 3, 'papers': {PAPER['id']: record([receipt['article']])}})
    assert export_results(outgoing, collected) == 1
    merge_results(outgoing, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['url'] == COPY['url'] and row['source_type'] == 'public_account_copy'
    assert row['display_mode'] == 'source' and not row['blocks']
    assert 'account_evidence' not in str(read(current / 'data/coverage-reviews' / (receipt['id'] + '.json')))


def test_all_receipts_validate_before_any_destination_write(tmp_path):
    collected, outgoing, current = tmp_path / 'collect', tmp_path / 'artifact', tmp_path / 'checkout'
    setup(collected); setup(current)
    good = approve(collected)
    snapshot(outgoing, {})
    write(outgoing / 'coverage-reviews' / (good['id'] + '.json'), good)
    bad = deepcopy(good)
    bad['id'] = 'f' * 64
    write(outgoing / 'coverage-reviews' / (bad['id'] + '.json'), bad)
    with pytest.raises(ValueError):
        merge_results(outgoing, current)
    assert not (current / coverage.DATA).exists()
    assert not list((current / 'data/coverage-reviews').glob('*.json'))


def content_article(version, title, when=NOW, **changes):
    return article(title=title, verified_at=when.isoformat(), source_version=version * 64,
        summary='研究团队提出可验证的新方法，并通过独立测试比较性能、适用范围和研究局限，提供清晰可靠的原始证据。' + title,
        summary_status='ready', summary_content_version=version * 64,
        display_mode='reprint', blocks=[{'type': 'heading', 'text': title},
                                       {'type': 'paragraph', 'text': 'Authorized body ' + version}], **changes)


@pytest.mark.parametrize('verification_time', [NOW, NOW + timedelta(hours=1)])
def test_later_complete_body_replaces_summary_version_and_authorized_structure(tmp_path, verification_time):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    grant(current)
    later = NOW + timedelta(hours=1)
    old, new = content_article('a', '旧报道'), content_article('b', '更新报道', verification_time)
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([old])}})
    snapshot(incoming, {PAPER['id']: record([{**new, 'raw_html': 'PRIVATE RAW',
        'transcript': 'PRIVATE TRANSCRIPT', 'blocks': new['blocks'] + [
            {'type': 'script', 'text': 'PRIVATE SCRIPT'}, {'type': 'image', 'src': 'PRIVATE IMAGE'}]}], later)})
    merge_results(incoming, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['title'] == '更新报道' and row['source_version'] == 'b' * 64
    assert row['summary'] == new['summary'] and row['summary_content_version'] == 'b' * 64
    assert row['display_mode'] == 'reprint' and row['blocks'] == new['blocks']
    assert 'PRIVATE' not in str(read(current / coverage.DATA))


@pytest.mark.parametrize('incoming_verification', [NOW, NOW + timedelta(days=2)])
def test_older_snapshot_never_rolls_back_new_content_even_with_later_verification(tmp_path, incoming_verification):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    grant(current)
    later = NOW + timedelta(hours=1)
    latest = content_article('b', '当前报道', later)
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([latest], later)}})
    snapshot(incoming, {PAPER['id']: record([content_article('a', '过期快照', incoming_verification)])})
    merge_results(incoming, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['title'] == latest['title'] and row['source_version'] == latest['source_version']
    assert row['summary'] == latest['summary'] and row['blocks'] == latest['blocks']


def test_newer_scan_cannot_replace_body_with_older_identity_version(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    grant(current)
    later = NOW + timedelta(hours=1)
    latest = content_article('b', '当前报道', later)
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([latest], later)}})
    snapshot(incoming, {PAPER['id']: record([content_article('a', '旧身份正文')], later + timedelta(hours=1))})
    merge_results(incoming, current)
    row, = coverage.load_public(current)[PAPER['id']]['articles']
    assert row['source_version'] == 'b' * 64 and row['blocks'] == latest['blocks']


def test_later_captcha_updates_access_without_discarding_proved_identity_or_saved_body(tmp_path):
    current, incoming = tmp_path / 'checkout', tmp_path / 'result'
    setup(current)
    grant(current)
    old = content_article('a', '已核验报道')
    write(current / coverage.DATA, {'papers': {PAPER['id']: record([old])}})
    later = NOW + timedelta(hours=1)
    snapshot(incoming, {PAPER['id']: record([article(title='验证码', body_status='challenge',
        source_version='c' * 64)], later)})
    merge_results(incoming, current)
    record_result = coverage.load_public(current)[PAPER['id']]
    row, = record_result['articles']
    assert record_result['state'] == 'matched' and row['match_status'] == 'verified'
    assert row['body_status'] == 'challenge' and row['source_version'] == 'a' * 64
    assert row['title'] == old['title'] and row['summary'] == old['summary'] and row['blocks'] == old['blocks']
    assert row['display_mode'] == 'reprint'


def test_official_crosslink_updated_receipt_survives_export_import(tmp_path):
    from tests.test_coverage_official_crosslink import (PAPER as LINKED_PAPER, REPORT, INDEX,
                                                       approve as approve_linked)
    collected, outgoing, current = tmp_path / 'collect', tmp_path / 'artifact', tmp_path / 'checkout'
    setup(collected, [LINKED_PAPER]); setup(current, [LINKED_PAPER])
    first = approve_linked(current)
    write(current / coverage.DATA, {'papers': {LINKED_PAPER['id']: record([first['article']])}})
    updated_report = {**REPORT, 'html': REPORT['html'] + '<!-- fetched newer version -->'}
    later = NOW + timedelta(hours=1)
    second = approve_linked(collected, report=updated_report)
    assert second['source_version'] != first['source_version']
    write(collected / coverage.DATA, {'papers': {LINKED_PAPER['id']: record([
        {**second['article'], 'body_status': 'unavailable', 'raw_html': 'PRIVATE HTML'}], later)}})
    assert export_results(outgoing, collected) == 1
    assert 'PRIVATE HTML' not in str(read(outgoing / 'paper-coverage.json'))
    merge_results(outgoing, current)
    row, = coverage.load_public(current)[LINKED_PAPER['id']]['articles']
    assert row['verification_method'] == 'official_crosslink' and row['account'] == ''
    assert row['account_status'] == 'not_observed' and row['source_version'] == second['source_version']
    assert row['body_status'] == 'unavailable' and row['mirrors'][0]['url'] == REPORT['url']
    assert row['blocks'] == [] and row['summary'] == '' and row['display_mode'] == 'source'
    assert INDEX['url'] not in str(read(outgoing / 'paper-coverage.json'))
    assert (current / 'data/coverage-reviews' / (second['id'] + '.json')).exists()
