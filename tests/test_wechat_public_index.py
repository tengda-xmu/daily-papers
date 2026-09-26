import io
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from src.models import RawRecord, SourceStatus
from src.sources import wechat_public_index as index
from src.sources.wechat import WeChatAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
from src.wechat_metadata import public_export, public_records


NOW = datetime(2026, 9, 23, 9, tzinfo=timezone.utc)
WINDOW = (NOW - timedelta(days=30), NOW)


def result_html(title="结构可靠性与智能体", account="研究号", date=NOW - timedelta(days=1)):
    return f'''<ul class="news-list"><li id="sogou_vr_11002601_box_0">
    <h3><a href="/link?token=PRIVATE">{title}<em>分析</em></a></h3>
    <p class="txt-info">研究摘要 <em>短片段</em></p>
    <span class="all-time-y2"><a>{account}</a></span>
    <script>document.write(timeConvert('{int(date.timestamp())}'));</script></li></ul>'''


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/wechat_accounts.json").write_text(json.dumps({"seed_names": ["研究号"]}), encoding="utf-8")
    monkeypatch.setattr(index, "ROOT", tmp_path)
    monkeypatch.setattr(index.time, "sleep", lambda *_: None)
    # Every test must explicitly supply a response; never use a real website.
    def unexpected(*args, **kwargs):
        raise AssertionError("Unexpected network request")
    monkeypatch.setattr(index, "urlopen", unexpected)
    return index.WeChatPublicIndexAdapter(queries=["query-one"], cache_dir=tmp_path / "cache")


def test_public_results_preserve_dates_and_never_follow_redirects(adapter, monkeypatch):
    calls = []
    def respond(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO((result_html() + result_html(account="未订阅号") +
                           result_html(date=NOW - timedelta(days=60))).encode())
    monkeypatch.setattr(index, "urlopen", respond)
    rows = adapter.fetch(*WINDOW)
    assert len(rows) == 1 and rows[0].venue == "研究号"
    assert rows[0].published_at == (NOW - timedelta(days=1)).isoformat()
    assert rows[0].title == "结构可靠性与智能体分析"
    assert rows[0].raw_metadata["abstract_kind"] == "search_snippet"
    assert "PRIVATE" not in rows[0].landing_url
    assert urlsplit(rows[0].landing_url).path == "/weixin"
    assert all(urlsplit(url).path == "/weixin" for url in calls)
    assert adapter.fetch(*WINDOW) == rows and len(calls) == 1


def test_homepage_and_captcha_are_not_successful_results():
    with pytest.raises(index.PublicSearchUnavailable):
        index.parse_search("<html>搜索首页</html>")
    with pytest.raises(index.PublicSearchChallenge):
        index.parse_search("请输入验证码")


def test_daily_request_budget_survives_repeated_runs(adapter, monkeypatch):
    calls = []
    def respond(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(result_html().encode())
    monkeypatch.setattr(index, "urlopen", respond)
    adapter.queries = ["one", "two", "three"]
    assert adapter.fetch(*WINDOW)
    adapter.queries = ["four", "five", "six"]
    assert not adapter.fetch(*WINDOW)
    assert len(calls) == 3 and "预算已用完" in adapter.status.message
    assert adapter.status.status == "quota_exhausted"
    adapter.queries = ["one"]
    assert adapter.fetch(*WINDOW) and len(calls) == 3


def test_daily_plan_is_shared_and_uses_beijing_day(adapter, monkeypatch):
    policy = {'public_article_queries': ['科研 {year}年{month}月'], 'public_daily_queries': 3}
    accounts = [{'name': '研究号', 'enabled': True}]
    before_midnight = datetime(2026, 9, 25, 17, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 9, 26, 7, tzinfo=timezone.utc)
    queries = index.daily_queries(policy, accounts, before_midnight)
    assert queries == index.daily_queries(policy, accounts, after_midnight)
    (index.ROOT / 'config/wechat_accounts.json').write_text(json.dumps(policy), encoding='utf8')
    monkeypatch.setattr(index, 'effective_accounts', lambda *_: accounts)
    calls = []
    def respond(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO(result_html().encode())
    monkeypatch.setattr(index, 'urlopen', respond)
    adapter.queries = None  # Local export.
    adapter.fetch(*WINDOW)
    shared = index.WeChatPublicIndexAdapter(cache_dir=adapter.cache_dir,
        queries=index.daily_queries(policy, accounts, NOW))  # Column collection.
    shared.fetch(*WINDOW)
    assert len(calls) == len(shared.queries)
    assert shared.collection['cached'] == shared.collection['completed'] == len(shared.queries)


def test_request_budget_rolls_over_at_beijing_midnight(adapter, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 16, 5, tzinfo=timezone.utc)
    monkeypatch.setattr(index, 'datetime', Clock)
    adapter.cache_dir.mkdir()
    budget = adapter.cache_dir / 'request-budget.json'
    budget.write_text(json.dumps({'day': '2026-09-25', 'count': 3}))
    monkeypatch.setattr(index, 'urlopen', lambda *a, **k: io.BytesIO(result_html().encode()))
    assert adapter.fetch(*WINDOW)
    assert json.loads(budget.read_text()) == {'day': '2026-09-26', 'count': 1, 'blocked_until': ''}


def test_query_failures_and_budget_deferrals_remain_distinct(adapter, monkeypatch):
    def respond(request, **kwargs):
        if request.full_url.endswith('bad'):
            raise TimeoutError('token=PRIVATE')
        return io.BytesIO(result_html().encode())
    monkeypatch.setattr(index, 'urlopen', respond)
    adapter.queries = ['one', 'bad', 'three']
    assert adapter.fetch(*WINDOW) and adapter.status.status == 'partial'
    assert adapter.collection['completed'] == 2 and adapter.collection['failed'] == 1
    assert adapter.collection['reasons'] == ['network_error']
    assert '连接失败或超时' in adapter.status.message and 'PRIVATE' not in adapter.status.message
    adapter.queries = ['one', 'four']
    adapter.fetch(*WINDOW)
    assert adapter.collection['cached'] == 1 and adapter.collection['failed'] == 0
    assert adapter.collection['deferred'] == 1 and adapter.collection['reasons'] == ['daily_limit']
    assert '1 项暂缓' in adapter.status.message


def test_manual_scope_reuses_raw_cache_without_subscription_allowlist(adapter, monkeypatch):
    calls = []
    def respond(request, **kwargs):
        calls.append(request.full_url)
        return io.BytesIO((result_html(account="未订阅的可靠性公众号") +
                           result_html(account="未订阅的可靠性公众号", date=NOW - timedelta(days=60))).encode())
    monkeypatch.setattr(index, "urlopen", respond)
    # The daily request still filters to subscribed accounts, caching all raw rows.
    assert adapter.fetch(*WINDOW) == []
    manual = index.WeChatPublicIndexAdapter(queries=adapter.queries,
        cache_dir=adapter.cache_dir, subscribed_only=False)
    rows = manual.fetch(*WINDOW)
    assert len(rows) == 1 and rows[0].venue == "未订阅的可靠性公众号"
    assert len(calls) == 1  # No extra network request or budget charge.
    assert "返回 2 条" in manual.status.message and "日期内 1 条" in manual.status.message
    assert "不限已订阅" in manual.status.message
    assert adapter.fetch(*WINDOW) == []  # Manual scope does not alter daily scope.


def test_manual_scope_does_not_require_subscription_files(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "ROOT", tmp_path)
    monkeypatch.setattr(index, "urlopen", lambda *a, **kw: io.BytesIO(result_html().encode()))
    manual = index.WeChatPublicIndexAdapter(queries=["Kriging"],
        cache_dir=tmp_path / "cache", subscribed_only=False)
    assert len(manual.fetch(*WINDOW)) == 1


def test_manual_merge_keeps_historical_export_when_live_feed_has_new_articles(tmp_path, monkeypatch):
    from src.sources import wechat_rss
    export = tmp_path / 'wechat.json'
    export.write_text(json.dumps({'exported_at': NOW.isoformat(), 'records': [
        {'title': 'Kriging surrogate', 'account': '历史号', 'published_at': (NOW - timedelta(days=10)).isoformat(),
         'landing_url': 'https://mp.weixin.qq.com/s/history'}]}), encoding='utf-8')
    live = json.dumps({'items': [{'title': 'Other research', 'published_at': NOW.isoformat(),
        'landing_url': 'https://mp.weixin.qq.com/s/current', 'account': '今日号'}]})
    monkeypatch.setattr(wechat_rss, 'urlopen', lambda *a, **kw: io.BytesIO(live.encode()))
    manual = WeChatRSSAdapter(urls=['https://rss.example/feed'], import_path=export, merge_import=True)
    assert {r.title for r in manual.fetch(*WINDOW)} == {'Kriging surrogate', 'Other research'}
    daily = WeChatRSSAdapter(urls=['https://rss.example/feed'], import_path=export)
    assert {r.title for r in daily.fetch(*WINDOW)} == {'Other research'}


def test_captcha_cooldown_persists_across_processes(adapter, monkeypatch):
    calls = []
    def respond(*args, **kwargs):
        calls.append(1)
        return io.BytesIO("请输入验证码".encode())
    monkeypatch.setattr(index, "urlopen", respond)
    adapter.queries = ["one", "two", "three"]
    assert adapter.fetch(*WINDOW) == [] and len(calls) == 1
    another = index.WeChatPublicIndexAdapter(cache_dir=adapter.cache_dir, queries=["four"])
    assert another.fetch(*WINDOW) == [] and len(calls) == 1
    assert another.status.status == "access_denied"
    budget = json.loads((adapter.cache_dir / "request-budget.json").read_text())
    assert datetime.fromisoformat(budget["blocked_until"]) > datetime.now(timezone.utc)


def test_public_export_rebuilds_link_without_private_fields():
    rows = public_records([{"title": "研究", "account": "研究号", "published_at": NOW.isoformat(),
        "access_mode": "public_index", "landing_url": "https://evil.test/?token=PRIVATE",
        "cookies": "PRIVATE", "raw_metadata": {"secret": "PRIVATE"}}])
    assert len(rows) == 1 and "PRIVATE" not in json.dumps(rows)
    assert urlsplit(rows[0]["landing_url"]).hostname == "weixin.sogou.com"
    payload = public_export({"exported_at": NOW.isoformat(), "records": rows})
    assert payload["provider"] == "WeChat public index"
    assert public_records([{"title": "fake", "landing_url": "https://evil.test/", "raw_metadata": []}]) == []


@pytest.mark.parametrize("code", [403, 429])
def test_public_http_restrictions_stop_remaining_requests(adapter, monkeypatch, code):
    from urllib.error import HTTPError
    calls = []
    def respond(*args, **kwargs):
        calls.append(1)
        raise HTTPError("https://weixin.sogou.com/weixin", code, "restricted", {}, None)
    monkeypatch.setattr(index, "urlopen", respond)
    adapter.queries = ["one", "two", "three"]
    assert adapter.fetch(*WINDOW) == [] and len(calls) == 1
    assert adapter.status.status == "access_denied"


def test_index_snapshot_roundtrip_keeps_native_restriction_distinct(tmp_path):
    exported = public_export({"exported_at": NOW.isoformat(), "records": [{"title": "研究", "account": "研究号",
        "published_at": (NOW - timedelta(days=1)).isoformat(), "access_mode": "public_index"}]})
    path = tmp_path / "wechat.json"
    path.write_text(json.dumps(exported), encoding="utf-8")
    path.with_name("wechat-status.json").write_text(json.dumps({"status": "quota_exhausted", "platform_code": "200013",
        "checked_at": (NOW - timedelta(hours=1)).isoformat(), "accounts": ["研究号"], "authenticated": True}), encoding="utf-8")
    rss = WeChatRSSAdapter(urls=[], import_path=path)
    assert len(rss.fetch(*WINDOW)) == 1 and rss.status.status == "ok"
    assert rss.status.message.startswith("公开索引已接入") and "后台文章列表受限" in rss.status.message
    assert len(rss.fetch(WINDOW[0], NOW + timedelta(hours=25))) == 1 and rss.status.status == "partial"


def test_fresh_import_skips_search_and_stale_import_merges_leads():
    class Adapter:
        def __init__(self, status, rows):
            self.status, self.rows, self.calls = SourceStatus("微信公众号", status), rows, 0
        def fetch(self, *args):
            self.calls += 1
            return self.rows
    row = RawRecord("微信公众号", "one", "旧文章", landing_url="https://example.test/1")
    imported, public = Adapter("ok", [row]), Adapter("ok", [])
    wrapper = WeChatAdapter(imported, public)
    assert wrapper.fetch(*WINDOW) == [row] and public.calls == 0
    imported.status.status = "partial"
    public.rows = [RawRecord("微信公众号", "two", "新文章", landing_url="https://example.test/2")]
    assert len(wrapper.fetch(*WINDOW)) == 2 and public.calls == 1


def test_public_collection_never_calls_restricted_native_endpoint_and_preserves_rows(tmp_path, monkeypatch):
    import connectors.wechat_sync.export as connector
    (tmp_path / "config").mkdir()
    (tmp_path / "config/wechat_accounts.json").write_text('{"article_mode":"public_index"}', encoding="utf-8")
    monkeypatch.setattr(connector, "ROOT", tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("Restricted native article endpoint must not be called")
    monkeypatch.setattr(connector, "refresh_local_feeds", forbidden)
    now = datetime.now(timezone.utc)
    class Index:
        status = SourceStatus("微信公众号", "ok", 1)
        def fetch(self, *args):
            return [RawRecord("微信公众号", "new", "新文章", venue="研究号", published_at=now.isoformat(),
                raw_metadata={"access_mode": "public_index"})]
    monkeypatch.setattr(connector, "WeChatPublicIndexAdapter", Index)
    output = tmp_path / "wechat.json"
    output.write_text(json.dumps({"records": [{"title": "旧文章", "account": "研究号", "access_mode": "public_index",
        "published_at": (now - timedelta(days=1)).isoformat()}]}), encoding="utf-8")
    assert connector.export(output, refresh=True) == 2
    assert {row["title"] for row in json.loads(output.read_text(encoding="utf-8"))["records"]} == {"旧文章", "新文章"}


def test_failed_public_refresh_publishes_health_without_replacing_articles(tmp_path, monkeypatch):
    import connectors.wechat_sync.export as connector
    from src.wechat_metadata import public_health
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config/wechat_accounts.json').write_text('{"article_mode":"public_index"}')
    monkeypatch.setattr(connector, 'ROOT', tmp_path)
    now = datetime.now(timezone.utc)
    exported = public_export({'exported_at': (now - timedelta(hours=1)).isoformat(), 'records': [
        {'title': '旧文章', 'account': '研究号', 'published_at': now.isoformat(), 'access_mode': 'public_index'}]})
    path = tmp_path / 'wechat.json'
    path.write_text(json.dumps(exported), encoding='utf8')
    before = path.read_bytes()
    class Index:
        status = SourceStatus('微信公众号', 'access_denied', 0)
        collection = {'provider': 'WeChat public index', 'status': 'access_denied', 'checked_at': now.isoformat(),
                      'planned': 3, 'completed': 0, 'failed': 1, 'deferred': 2, 'reasons': ['verification_required'],
                      'cookies': 'PRIVATE', 'error': 'token=PRIVATE'}
        def fetch(self, *args):
            return []
    monkeypatch.setattr(connector, 'WeChatPublicIndexAdapter', Index)
    with pytest.raises(RuntimeError):
        connector.export(path, refresh=True)
    assert path.read_bytes() == before
    health = json.loads(path.with_name('wechat-status.json').read_text('utf8'))
    assert public_health(health) == health and 'PRIVATE' not in json.dumps(health)
    imported = WeChatRSSAdapter(urls=[], import_path=path)
    assert len(imported.fetch(now - timedelta(days=30), now)) == 1
    assert imported.status.status == 'partial' and '人工验证' in imported.status.message
    assert '微信授权需要更新' not in imported.status.message
    # A subsequent successful export removes the failure without losing old records.
    Index.status = SourceStatus('微信公众号', 'ok', 1)
    Index.collection = {**Index.collection, 'status': 'ok', 'completed': 3, 'failed': 0, 'deferred': 0, 'reasons': [],
                        'checked_at': (now + timedelta(seconds=1)).isoformat()}
    Index.fetch = lambda *_: [RawRecord('微信公众号', 'new', '新文章', venue='研究号', published_at=now.isoformat(),
                                       raw_metadata={'access_mode': 'public_index'})]
    assert connector.export(path, refresh=True) == 2
    payload = json.loads(path.read_text('utf8'))
    assert payload['failed_feeds'] == 0 and public_export(payload)['collection']['completed'] == 3
    imported.fetch(now - timedelta(days=30), now + timedelta(minutes=1))
    assert imported.status.status == 'ok' and '3/3' in imported.status.message


def test_shared_status_and_current_page_follow_independent_public_updates():
    from src.sources.wechat import shared_snapshot
    from tools.build_site import current_public_sources, source_status_panel
    snapshot = {'checked_at': NOW.isoformat(), 'entries': [
        {'id': 'one', 'platform': 'wechat', 'title': '新线索', 'url': 'https://mp.weixin.qq.com/s/one',
         'published_at': NOW.isoformat()}], 'sources': [
        {'id': 'wechat-rss', 'name': '公众号同步', 'status': 'partial', 'message': '公开查询完成 2/3 项；连接失败或超时。'},
        {'id': 'wechat-index', 'name': '公开索引', 'status': 'ok', 'message': '复用缓存 3 项。'}]}
    rows, status = shared_snapshot(snapshot, *WINDOW)
    assert len(rows) == 1 and status.status == 'partial' and '连接失败或超时' in status.message
    original = {'generated_at': (NOW - timedelta(hours=1)).isoformat(), 'since': WINDOW[0].isoformat(),
                'core': [{'id': 'paper'}], 'extended': [], 'source_status': {'微信公众号': {'status': 'partial'}}}
    current = current_public_sources(original, snapshot)
    assert current['core'] == original['core'] and current['generated_at'] == original['generated_at']
    assert 'wechat_articles' not in original  # Historical edition not mutated.
    html = source_status_panel(current, reading_url='./')
    assert '连接失败或超时' in html and '复用缓存 3 项' in html and '本期 1 条线索' in html
    assert '部分期刊采集完成' not in html
    snapshot['sources'][0].update(status='ok', message='公开查询完成 3/3 项。')
    recovered = current_public_sources(original, snapshot)
    assert recovered['source_status']['微信公众号']['status'] == 'ok'
    assert current_public_sources(original, {**snapshot, 'checked_at': WINDOW[0].isoformat()}) is original


def test_wechat_leads_are_visible_but_never_promoted_to_paper_analysis(monkeypatch):
    from src.pipeline import run_pipeline
    from tools.build_site import render, render_leads, source_status_panel
    def forbidden(*args):
        raise AssertionError("Do not analyze search snippets as papers")
    monkeypatch.setattr("src.pipeline._llm_summary", forbidden)
    monkeypatch.setenv("LLM_API_KEY", "fixture")
    class Adapter:
        status = SourceStatus("微信公众号", "ok", 1, "公开索引已接入：微信后台文章列表受限。")
        def fetch(self, *args):
            return [RawRecord("微信公众号", "one", "LLM agents for structural fatigue reliability",
                venue="研究号", abstract="A search snippet " * 20, published_at=NOW.isoformat(),
                landing_url="https://weixin.sogou.com/weixin?type=2", raw_metadata={"access_mode": "public_index"})]
    payload = run_pipeline(*WINDOW, adapters=[Adapter()])
    assert len(payload["wechat_articles"]) == 1
    assert payload["core"] == payload["extended"] == payload["papers"] == []
    html = render(payload)
    assert 'id="wechat-articles"' not in html and 'id="sources"' not in html
    assert 'leads.html' in html and 'setup.html#sources' in html
    leads = render_leads(payload)
    assert 'id="wechat-articles"' in leads and '1 条线索' in leads
    assert "公开检索入口" in leads and "LLM agents for structural fatigue reliability" in leads
    sources = source_status_panel(payload, reading_url='./')
    assert "本期 1 条线索" in sources and "公开索引可用" in sources
    assert '<summary>数据采集<span>' in sources and 'href="./leads.html"' in sources
    archive = render(payload, archive_date='2026-09-24')
    assert 'id="wechat-articles"' in archive and 'id="sources"' in archive
