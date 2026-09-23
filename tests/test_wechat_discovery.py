import base64
import json
from datetime import datetime, timedelta, timezone

import pytest

from connectors.wechat_sync import discover as discovery
from src.wechat_metadata import public_subscriptions


@pytest.fixture
def policy():
    return discovery.read_json(discovery.CONFIG, {})


def row(name, signature="", number=123):
    return {"nickname": name, "signature": signature, "alias": "research", "fakeid": base64.b64encode(str(number).encode()).decode()}


@pytest.mark.parametrize("name,signature,eligible", [
    ("航空学报CJA", "航空航天学术成果", True),
    ("NLP PaperWeekly", "关注大模型、多模态等领域的论文", True),
    ("大模型推理", "专注大模型推理工程：KV Cache、量化、投机采样", True),
    ("智能体研究", "分享 AI Agent 与语言模型的开源算法", True),
    ("可靠性知识", "交流可靠性技术，服务产品可靠性设计", True),
    ("中小学人工智能", "人工智能教育推广和算法技术", False),
    ("CAAI会员中心", "中国人工智能学会会员服务", False),
    ("大模型赚钱", "智能体变现培训班", False),
    ("大模型", "喝酒读诗，随笔与生活", False),
    ("新智元生物", "专注AI与生物技术融合，蛋白质设计和生物医药创新", False),
])
def test_research_subscription_selection(policy, name, signature, eligible):
    assert discovery.evaluate(row(name, signature), policy, set())["eligible"] is eligible


def test_ambiguous_name_requires_review_even_for_seed(policy):
    result = discovery.evaluate(row("航空学报CJA"), policy, {discovery.normalized("航空学报CJA")})
    assert not result["eligible"] and result["reason"] == "ambiguous_name"
    assert not discovery.matches("AI", discovery.normalized("CAAI会员中心"))
    assert not discovery.account_id("invalid")


def setup_discovery(tmp_path, monkeypatch, policy, rows):
    policy.update(queries=["人工智能", "可靠性", "航空学报"], daily_queries=2, daily_additions=3)
    config = tmp_path / "policy.json"
    discovery.write_json(config, policy)
    calls, feeds = [], []
    def api(path, token="", json_body=None):
        calls.append((path, json_body))
        if path.startswith("/mps?"):
            return {"list": list(feeds)}
        if path.startswith("/mps/search/"):
            return {"list": rows}
        identity = discovery.account_id(json_body["mp_id"])
        feeds.append({"id": identity, "mp_name": json_body["mp_name"]})
        return {"id": identity}
    monkeypatch.setattr(discovery, "authenticated_session", lambda: "local-private-token")
    monkeypatch.setattr(discovery, "local_api", api)
    monkeypatch.setattr(discovery.time, "sleep", lambda _: None)
    return config, calls, feeds


def test_daily_discovery_bounds_deduplicates_and_defers_article_requests(tmp_path, monkeypatch, policy):
    rows = [row("智能体研究" + str(i), "AI Agent 大模型开源研究", i) for i in range(5)]
    config, calls, feeds = setup_discovery(tmp_path, monkeypatch, policy, rows)
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    result = discovery.discover(config, state, snapshot)
    assert len(result["added"]) == 3 and len(feeds) == 3
    assert all(body["fetch_articles"] is False for _, body in calls if body)
    assert not any("/update/" in path for path, _ in calls)
    count = len(calls)
    assert discovery.discover(config, state, snapshot)["status"] == "not_due"
    assert len(calls) == count
    saved = discovery.read_json(state, {})
    assert sum(c["eligible"] and not c["subscribed"] for c in saved["candidates"].values()) == 2
    saved["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    discovery.write_json(state, saved)
    result = discovery.discover(config, state, snapshot)
    assert len(result["added"]) == 2 and len(feeds) == 5


def test_search_budget_and_cursor_rotate(tmp_path, monkeypatch, policy):
    config, calls, _ = setup_discovery(tmp_path, monkeypatch, policy, [])
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    discovery.discover(config, state, snapshot)
    assert sum("/search/" in path for path, _ in calls) == 2
    assert discovery.read_json(state, {})["cursor"] == 2


def test_search_rate_limit_stops_and_persists_cooldown(tmp_path, monkeypatch, policy):
    config, calls, _ = setup_discovery(tmp_path, monkeypatch, policy, [])
    def api(path, token="", **kwargs):
        calls.append((path, None))
        if "/search/" in path:
            raise discovery.WeChatRateLimited("200013")
        return {"list": []}
    monkeypatch.setattr(discovery, "local_api", api)
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    result = discovery.discover(config, state, snapshot)
    assert result["status"] == "quota_exhausted"
    assert sum("/search/" in path for path, _ in calls) == 1
    saved = discovery.read_json(state, {})
    assert datetime.fromisoformat(saved["next_run_at"]) - datetime.fromisoformat(saved["last_run_at"]) >= timedelta(hours=24)
    assert discovery.read_json(snapshot, {})["discovery"]["status"] == "quota_exhausted"


def test_public_subscription_snapshot_never_contains_sessions_or_search_cache():
    payload = {"updated_at": "2026-09-23T04:00:00Z", "cookies": "private", "cache": "private",
               "accounts": [{"name": "航空学报CJA", "alias": "HKXBCJA", "groups": ["航空航天"], "token": "private"}],
               "discovery": {"status": "ok", "enabled": True, "daily_queries": 2, "daily_additions": 3, "max_subscriptions": 60}}
    result = public_subscriptions(payload)
    assert "private" not in json.dumps(result) and result["accounts"][0]["name"] == "航空学报CJA"


def test_authorization_expiry_preserves_existing_directory(tmp_path, monkeypatch, policy):
    config, _, _ = setup_discovery(tmp_path, monkeypatch, policy, [])
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    discovery.write_json(snapshot, {"accounts": [{"name": "航空学报CJA"}], "discovery": {"enabled": True}})
    def expired():
        raise discovery.WeChatAuthorizationRequired("expired")
    monkeypatch.setattr(discovery, "authenticated_session", expired)
    assert discovery.discover(config, state, snapshot)["status"] == "access_denied"
    assert discovery.read_json(snapshot, {})["accounts"] == [{"name": "航空学报CJA"}]


def test_feed_refresh_rotates_oldest_with_a_bounded_batch(tmp_path, monkeypatch):
    import connectors.wechat_sync.export as connector
    feeds = [{"id": str(i), "mp_name": "订阅" + str(i), "sync_time": i} for i in range(20)]
    updates = []
    def api(path, token="", form=None, timeout=45):
        if path == "/auth/login": return {"access_token": "local"}
        if path.startswith("/mps?"): return {"list": list(reversed(feeds))}
        if path == "/auth/qr/status": return {"login_status": True}
        updates.append(path.rsplit("/", 1)[-1])
        return {"total": 1}
    monkeypatch.setattr(connector, "ROOT", tmp_path)
    monkeypatch.setattr(connector, "local_api", api)
    monkeypatch.setattr(connector, "read_env", lambda *a: {"WERSS_USERNAME": "local", "WERSS_PASSWORD": "private"})
    assert connector.refresh_local_feeds() == 0
    assert updates == [str(i) for i in range(12)]


def test_capacity_preserves_candidates_without_subscribing(tmp_path, monkeypatch, policy):
    policy["max_subscriptions"] = 1
    config, calls, _ = setup_discovery(tmp_path, monkeypatch, policy,
        [row("智能体研究一", "大模型开源研究", 1), row("智能体研究二", "大模型开源研究", 2)])
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    result = discovery.discover(config, state, snapshot)
    assert result["status"] == "capacity_reached" and result["subscribed_count"] == 1
    assert discovery.read_json(snapshot, {})["discovery"]["pending_candidates"] == 1


def test_query_rotation_visits_more_than_the_first_page(tmp_path, monkeypatch, policy):
    config, calls, _ = setup_discovery(tmp_path, monkeypatch, policy, [])
    saved_policy = discovery.read_json(config, {})
    saved_policy["queries"] = ["可靠性"]
    discovery.write_json(config, saved_policy)
    state, snapshot = tmp_path / "state.json", tmp_path / "subscriptions.json"
    for _ in range(3):
        discovery.discover(config, state, snapshot)
        saved = discovery.read_json(state, {})
        saved["next_run_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        discovery.write_json(state, saved)
    searches = [path for path, _ in calls if "/search/" in path]
    assert len(searches) == 3
    assert all(path.endswith("offset=" + str(i * 5)) for i, path in enumerate(searches))


def test_pending_collection_stops_remaining_accounts(monkeypatch):
    import connectors.wechat_sync.export as connector
    updates = []
    def api(path, token="", form=None, timeout=45):
        if path == "/auth/login": return {"access_token": "local"}
        if path.startswith("/mps?"): return {"list": [{"id": "one"}, {"id": "two"}]}
        if path == "/auth/qr/status": return {"login_status": True}
        updates.append(path)
        return {"status": "processing"}
    monkeypatch.setattr(connector, "local_api", api)
    monkeypatch.setattr(connector, "read_env", lambda *a: {"WERSS_USERNAME": "local", "WERSS_PASSWORD": "private"})
    with pytest.raises(connector.WeChatCollectionPending):
        connector.refresh_local_feeds()
    assert len(updates) == 1
