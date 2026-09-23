import io
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError

import pytest

from src.sources.wechat_rss import WeChatRSSAdapter
from src.wechat_metadata import article_url, public_export
from connectors.wechat_sync.export import export


DATES = datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc)
URL = "https://mp.weixin.qq.com/s/public-paper"


def test_werss_json_preserves_account_and_real_publication_date():
    body = json.dumps({"items": [{"title": "智能体故障诊断", "channel_name": "研究号", "link": URL,
                                  "updated": "2026-09-22T08:00:00+08:00", "description": "文章短摘要", "content": "private full text"}]})
    row = WeChatRSSAdapter.parse(body)[0]
    assert row.venue == "研究号" and row.published_at == "2026-09-22T08:00:00+08:00"
    assert "private full text" not in json.dumps(row.to_dict())
    standard = WeChatRSSAdapter.parse(json.dumps({"items": [{"title": "Article", "date_published": "2026-09-21", "url": URL}]}))[0]
    assert standard.published_at == "2026-09-21"


def test_atom_uses_alternate_link_and_nested_author():
    body = f'''<feed xmlns="http://www.w3.org/2005/Atom"><title>订阅汇总</title><entry><title>论文解读</title>
    <link rel="self" href="http://localhost/private?token=secret"/><link rel="alternate" href="{URL}"/>
    <author><name>学术公众号</name></author><published>2026-09-22T10:00:00Z</published><summary>简介</summary></entry></feed>'''
    row = WeChatRSSAdapter.parse(body)[0]
    assert row.landing_url == URL and row.venue == "学术公众号"


@pytest.mark.parametrize("url", ["https://mp.weixin.qq.com.evil.test/s/a", "https://mp.weixin.qq.com/login",
                                    "https://private:password@mp.weixin.qq.com/s/a", "http://localhost/feed", "https://mp.weixin.qq.com/s?token=secret"])
def test_non_article_or_untrusted_urls_are_rejected(url):
    assert article_url(url) == ""


def test_public_export_strips_sessions_tracking_and_full_text():
    link = "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=def&appmsg_token=secret&key=private&uin=someone"
    clean = public_export({"exported_at": "2026-09-22T08:00:00Z", "cookies": "secret-cookie", "records": [
        {"title": "研究文章", "url": link, "account": "研究号", "published_at": "2026-09-22", "summary": "<p>" + "摘" * 500 + "</p>",
         "content": "private full text", "api_key": "secret-key", "raw_metadata": {"password": "secret"}}
    ]})
    text = json.dumps(clean)
    assert not any(term in text for term in ("secret", "private", "someone", "content", "raw_metadata"))
    row = clean["records"][0]
    assert len(row["summary"]) == 301 and row["landing_url"].endswith("__biz=abc&mid=123&idx=1&sn=def")
    assert row["published_at"].startswith("2026-09-22")


def test_one_bad_feed_does_not_block_other_feeds_or_date_filter(tmp_path, monkeypatch):
    calls = []
    def response(request, timeout):
        calls.append(request.full_url)
        if "bad" in request.full_url:
            raise URLError("secret url " + request.full_url)
        return io.BytesIO(json.dumps({"items": [{"title": "Current", "link": URL, "updated": "2026-09-22"},
                                                {"title": "Old", "link": URL + "-old", "updated": "2020-01-01"}]}).encode())
    monkeypatch.setattr("src.sources.wechat_rss.urlopen", response)
    adapter = WeChatRSSAdapter(urls=["https://bad.test/?token=secret", "https://good.test"], import_path=tmp_path / "none.json")
    rows = adapter.fetch(*DATES)
    assert len(calls) == 2 and [r.title for r in rows] == ["Current"]
    assert adapter.status.status == "partial" and "secret" not in adapter.status.message


def test_local_export_fallback_reports_freshness(tmp_path, monkeypatch):
    path = tmp_path / "wechat.json"
    path.write_text(json.dumps(public_export({"exported_at": "2026-09-22T08:00:00Z", "records": [
        {"title": "研究文章", "landing_url": URL, "published_at": "2026-09-22", "summary": "简介", "account": "研究号"}
    ]})), encoding="utf-8")
    adapter = WeChatRSSAdapter(urls=[], import_path=path)
    rows = adapter.fetch(*DATES)
    assert len(rows) == 1 and rows[0].venue == "研究号" and adapter.status.status == "ok"
    assert adapter.status.message.startswith("WeRSS 本地同步已接入")
    adapter.fetch(DATES[0], datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert adapter.status.status == "partial" and "超过 3 天" in adapter.status.message
    def failed(request, timeout):
        raise URLError("token=secret")
    monkeypatch.setattr("src.sources.wechat_rss.urlopen", failed)
    adapter = WeChatRSSAdapter(urls=["https://bad.test"], import_path=path)
    assert len(adapter.fetch(*DATES)) == 1 and adapter.status.status == "partial"


def test_empty_failed_collection_preserves_previous_export(tmp_path, monkeypatch):
    path = tmp_path / "wechat.json"
    path.write_text("previous metadata", encoding="utf-8")
    monkeypatch.setenv("WECHAT_RSS_URLS", "http://localhost/feed/all.json")
    monkeypatch.setattr("src.sources.wechat_rss.urlopen", lambda *a, **kw: io.BytesIO(b'{"items": []}'))
    with pytest.raises(RuntimeError):
        export(path)
    assert path.read_text() == "previous metadata"


def test_wechat_publisher_only_changes_its_branch_file(tmp_path, monkeypatch):
    import tools.publish_wechat as publisher
    from types import SimpleNamespace
    path = tmp_path / "wechat.json"
    path.write_text(json.dumps({"exported_at": "2026-09-22T08:00:00Z", "cookies": "secret", "records": [
        {"title": "AI维护", "landing_url": URL, "published_at": "2026-09-22"}
    ]}), encoding="utf-8")
    calls = []
    def git(*args, data=None, env=None):
        calls.append((args, data))
        output = {"ls-remote": "existing connector branch", "rev-parse": "parent", "hash-object": "blob", "write-tree": "tree", "commit-tree": "commit"}.get(args[0], "")
        return SimpleNamespace(stdout=output)
    monkeypatch.setattr(publisher, "ROOT", tmp_path)
    monkeypatch.setattr(publisher, "git", git)
    publisher.publish(path)
    assert (('read-tree', 'parent'), None) in calls
    updates = [args for args, _ in calls if args[0] == "update-index"]
    assert len(updates) == 1 and updates[0][-1] == "data/inbox/wechat.json"
    assert all("secret" not in (data or "") for _, data in calls)
    assert calls[-1][0] == ('push', 'origin', 'commit:refs/heads/connector-data')


def test_health_reports_real_rate_limit_without_fake_articles(tmp_path):
    from src.wechat_metadata import public_health
    payload = public_health({"checked_at": "2026-09-23T04:13:08Z", "status": "quota_exhausted",
                             "authenticated": True, "platform_code": 200013, "accounts": ["研究号"], "cookies": "private"})
    (tmp_path / "wechat-status.json").write_text(json.dumps(payload), encoding="utf-8")
    adapter = WeChatRSSAdapter(urls=[], import_path=tmp_path / "wechat.json")
    assert adapter.fetch(*DATES) == []
    assert adapter.status.status == "quota_exhausted" and "200013" in adapter.status.message
    assert "授权已完成" in adapter.status.message and "private" not in json.dumps(payload)


def test_rate_limit_stops_remaining_accounts_and_preserves_export(tmp_path, monkeypatch):
    import connectors.wechat_sync.export as connector
    monkeypatch.setattr(connector, "ROOT", tmp_path)
    calls = []
    def api(path, token="", form=None, timeout=45):
        calls.append(path)
        if path == "/auth/login": return {"access_token": "secret"}
        if path.startswith("/mps?"): return {"list": [{"id": "one", "mp_name": "号一"}, {"id": "two", "mp_name": "号二"}]}
        if path == "/auth/qr/status": return {"login_status": True}
        raise connector.WeChatRateLimited("200013")
    monkeypatch.setattr(connector, "local_api", api)
    monkeypatch.setattr(connector, "read_env", lambda *a: {"WERSS_USERNAME": "local", "WERSS_PASSWORD": "private"})
    output = tmp_path / "wechat.json"
    output.write_text("previous export")
    with pytest.raises(connector.WeChatRateLimited):
        connector.export(output, refresh=True)
    assert output.read_text() == "previous export"
    assert not any("/update/two" in call for call in calls)
    health = json.loads((tmp_path / "wechat-status.json").read_text(encoding="utf-8"))
    assert health["status"] == "quota_exhausted" and health["accounts"] == ["号一", "号二"]
    assert "private" not in json.dumps(health) and "secret" not in json.dumps(health)


def test_service_rate_limit_response_becomes_connector_rate_limit(monkeypatch):
    import connectors.wechat_sync.export as connector
    payload = {"code": 50001, "message": "Collection failed", "data": {"error": "WeChat rate limited (200013)"}}
    monkeypatch.setattr(connector, "urlopen", lambda *a, **kw: io.BytesIO(json.dumps(payload).encode()))
    with pytest.raises(connector.WeChatRateLimited):
        connector.local_api("/mps/update/example", "private-admin-token")


def test_authorization_restoration_waits_for_local_startup(tmp_path, monkeypatch):
    import connectors.wechat_sync.export as connector
    states = iter([False, False, True])
    waits = []
    def api(path, token="", form=None, timeout=45):
        if path == "/auth/login": return {"access_token": "secret"}
        if path.startswith("/mps?"): return {"list": [{"id": "one", "mp_name": "研究号"}]}
        if path == "/auth/qr/status": return {"login_status": next(states)}
        return {"total": 1}
    monkeypatch.setattr(connector, "local_api", api)
    monkeypatch.setattr(connector, "read_env", lambda *a: {"WERSS_USERNAME": "local", "WERSS_PASSWORD": "private"})
    monkeypatch.setattr(connector.time, "sleep", waits.append)
    health = tmp_path / "wechat-status.json"
    assert connector.refresh_local_feeds(health) == 0
    assert waits == [5, 5] and json.loads(health.read_text(encoding="utf-8"))["authenticated"] is True
