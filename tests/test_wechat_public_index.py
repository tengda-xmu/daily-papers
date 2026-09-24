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


def test_wechat_leads_are_visible_but_never_promoted_to_paper_analysis(monkeypatch):
    from src.pipeline import run_pipeline
    from tools.build_site import render
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
    assert 'id="wechat-articles"' in html and "本期 1 条线索" in html and "公开索引可用" in html
    assert '<summary>数据采集<span>' in html
    assert "公开检索入口" in html and "LLM agents for structural fatigue reliability" in html
