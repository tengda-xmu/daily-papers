import json
from datetime import datetime, timezone

import pytest

from src.research_leads import ROOT, collect, parse_feed, refresh, state

NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
FEED = {'id': 'official', 'name': 'Official', 'url': 'https://example.org/feed/', 'hosts': ['example.org'], 'topics': ['generative_design']}


def rss(*items):
    return ('<rss><channel>' + ''.join(f'<item><title>{title}</title><link>{url}</link><pubDate>{date}</pubDate>'
            '<description>&lt;b&gt;A short announcement&lt;/b&gt;</description></item>'
            for title, url, date in items) + '</channel></rss>').encode()


def test_feed_dates_excerpt_and_primary_source_allowlist():
    rows = parse_feed(rss(
        ('Call for papers', 'https://example.org/call', 'Thu, 24 Sep 2026 08:00:00 +0000'),
        ('old', 'https://example.org/old', '2025-01-01'),
        ('future', 'https://example.org/future', '2027-01-01'),
        ('undated', 'https://example.org/missing', '2026'),
        ('third party', 'https://elsewhere.org/ad', '2026-09-24'),
        ('unsafe', 'javascript:alert(1)', '2026-09-24'),
    ), FEED, NOW)
    assert len(rows) == 1
    assert rows[0]['kind'] == 'call'
    assert rows[0]['summary'] == 'A short announcement'
    assert rows[0]['published_at'] == '2026-09-24T08:00:00+00:00'
    atom = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Conference</title><link href="https://example.org/atom"/><updated>2026-09-23T10:00:00Z</updated></entry></feed>'
    assert parse_feed(atom, FEED, NOW)[0]['url'] == 'https://example.org/atom'
    for invalid in (b'<html>access denied</html>', b'<!DOCTYPE rss><rss/>'):
        with pytest.raises(ValueError):
            parse_feed(invalid, FEED, NOW)


def test_refresh_keeps_previous_on_failure_and_empty_then_expires(tmp_path):
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/research-leads.json').write_text(json.dumps({'feeds': [FEED]}))
    body = rss(('A new announcement', 'https://example.org/one', '2026-09-23'))
    first = refresh(tmp_path, now=NOW, fetch=lambda _: body)
    assert len(first['entries']) == 1
    def broken(_):
        raise OSError('private-key-should-not-be-exported')
    failed = refresh(tmp_path, now=NOW, fetch=broken)
    assert failed['entries'] == first['entries']
    assert failed['sources'][0]['status'] == 'error'
    assert 'private-key' not in json.dumps(failed)
    empty = refresh(tmp_path, now=NOW, fetch=lambda _: rss())
    assert empty['entries'] == first['entries']
    assert empty['sources'][0]['status'] == 'no_data'
    expired = refresh(tmp_path, now=datetime(2027, 1, 1, tzinfo=timezone.utc), fetch=broken)
    assert expired['entries'] == []


def test_event_status_is_not_submission_status_and_aoe_is_respected():
    assert state({'start': '2026-09-26', 'end': '2026-09-30'}, NOW)[0] == 'upcoming'
    assert state({'start': '2026-09-23', 'end': '2026-09-25'}, NOW)[0] == 'ongoing'
    assert state({'end': '2026-09-23'}, NOW)[0] == 'ended'
    assert state({'kind': 'conference'}, NOW) == ('unknown', '会期见原文')
    assert state({'kind': 'call'}, NOW) == ('unknown', '投稿状态见原文')
    assert state({'deadline': '2026-11-30', 'opens': '2026-10-15'}, NOW)[0] == 'planned'
    call = {'deadline': '2026-09-25', 'deadline_at': '2026-09-25T23:59:00-12:00'}
    assert state(call, datetime(2026, 9, 26, 10, tzinfo=timezone.utc))[0] == 'deadline'
    assert state(call, datetime(2026, 9, 26, 12, tzinfo=timezone.utc))[0] == 'ended'


def test_history_dedup_private_data_excluded_and_snapshot_not_mutated(tmp_path):
    (tmp_path / 'data/archive').mkdir(parents=True)
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/research-directions.json').write_bytes((ROOT / 'config/research-directions.json').read_bytes())
    event = {'id': 'conference', 'kind': 'conference', 'title': 'PHM', 'url': 'https://example.org/event',
             'start': '2026-10-01', 'end': '2026-10-03', 'topics': ['ai_maintenance']}
    (tmp_path / 'config/research-leads.json').write_text(json.dumps({'entries': [event]}))
    article = {'title': 'Digital twin research', 'venue': 'test', 'abstract': 'Old snippet', 'published_at': '2026-09-23',
               'landing_url': 'https://mp.weixin.qq.com/s/article', 'raw_metadata': {'private': 'not-public'}}
    (tmp_path / 'data/archive/2026-09-23.json').write_text(json.dumps({'wechat_articles': [article]}))
    private = tmp_path / '.local/wechat-subscriptions'
    private.mkdir(parents=True)
    (private / 'state.json').write_text('{"secret": "private-only"}')
    payload = {'core': [{'id': 'keep'}], 'wechat_articles': [{**article, 'abstract': 'Updated snippet'}]}
    before = json.dumps(payload)
    data = collect(payload, tmp_path, now=NOW)
    assert len(data['entries']) == 2
    assert data['entries'][1]['summary'] == 'Updated snippet'
    assert 'ai_maintenance' in data['entries'][1]['topics']
    assert 'private' not in json.dumps(data)
    assert json.dumps(payload) == before


def test_render_filters_dates_and_escapes_source_content():
    from tools.build_site import render_leads
    item = {'id': 'test', 'kind': 'conference', 'title': '<script>evil</script>', 'source': 'Fixture',
            'url': 'javascript:alert(1)', 'summary': '<img src=x>', 'start': '2027-01-01', 'end': '2027-01-02'}
    html = render_leads({}, {'entries': [item], 'sources': [], 'directions': [], 'days': 90})
    assert '<script>evil' not in html and 'href="javascript:' not in html
    assert '&lt;script&gt;' in html and '&lt;img src=x&gt;' in html
    assert 'id="lead-pagination"' in html and 'id="lead-topic"' in html
    assert '会期 2027-01-01 至 2027-01-02' in html


def test_curated_event_catalog_integrity():
    config = json.loads((ROOT / 'config/research-leads.json').read_text(encoding='utf-8'))
    rows = config['entries']
    assert len({r['id'] for r in rows}) == len(rows)
    assert len([r for r in rows if r['kind'] == 'conference']) >= 10
    for row in rows:
        assert row['url'].startswith('https://') and row['verified_at']
        if row['kind'] == 'conference':
            assert row['start'] <= row['end']
