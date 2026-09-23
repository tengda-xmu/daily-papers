import json
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from src.sources.public_literature import WebOfScienceAdapter
from tools import connect_wos

WINDOW = (datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc))
HIT = {"uid": "WOS:1", "title": "Large language models for structural fatigue",
       "source": {"sourceTitle": "Nature Communications", "publishYear": 2026},
       "identifiers": {"doi": "10.1000/example"}, "citations": []}


def test_wos_uses_official_date_bounds_and_free_plan_request_spacing(monkeypatch):
    calls, waits = [], []
    adapter = WebOfScienceAdapter(api_key="private-fixture")
    def get(url, headers):
        calls.append((url, headers))
        return {"hits": [HIT]}
    monkeypatch.setattr(adapter, "_get_json", get)
    monkeypatch.setattr("src.sources.public_literature.time.sleep", waits.append)
    records = adapter.fetch(*WINDOW)
    assert len(calls) == 3 and len(records) == 1
    assert waits == [1.1, 1.1]
    query = parse_qs(urlsplit(calls[0][0]).query)
    assert query["publishTimeSpan"] == ["2026-09-01+2026-09-23"]
    assert query["db"] == ["WOS"] and query["sortField"] == ["LD+D"]
    assert all("private-fixture" not in url and headers["X-ApiKey"] == "private-fixture" for url, headers in calls)
    assert records[0].citation_count is None
    assert "Starter API 已授权" in adapter.status.message


@pytest.mark.parametrize("code,state", [(401, "authorization_required"), (403, "access_denied"), (429, "quota_exhausted")])
def test_authorization_and_quota_failures_stop_requests_without_leaking_key(monkeypatch, code, state):
    adapter = WebOfScienceAdapter(api_key="private-fixture")
    calls = []
    def fail(*args):
        calls.append(1)
        raise HTTPError("https://example.test/?key=private-fixture", code, "private-fixture", {}, None)
    monkeypatch.setattr(adapter, "_get_json", fail)
    assert adapter.fetch(*WINDOW) == [] and len(calls) == 1
    assert adapter.status.status == state
    assert "private-fixture" not in json.dumps(adapter.status.to_dict())


def test_later_query_failure_keeps_only_current_unique_wos_records(monkeypatch):
    adapter = WebOfScienceAdapter(api_key="fixture")
    calls = []
    def get(*args):
        calls.append(1)
        if len(calls) > 1:
            raise HTTPError("https://example.test/", 429, "quota", {}, None)
        return {"hits": [HIT, HIT, {**HIT, "uid": "WOS:old", "identifiers": {"doi": "10/old"}, "source": {"publishYear": 2020}}]}
    monkeypatch.setattr(adapter, "_get_json", get)
    monkeypatch.setattr("src.sources.public_literature.time.sleep", lambda *_: None)
    assert len(adapter.fetch(*WINDOW)) == 1 and adapter.status.status == "partial"


def test_invalid_success_payload_is_not_reported_as_authorized(monkeypatch):
    adapter = WebOfScienceAdapter(api_key="fixture")
    monkeypatch.setattr(adapter, "_get_json", lambda *_: {"error": "invalid_request"})
    assert adapter.fetch(*WINDOW) == [] and adapter.status.status == "error"


def test_connection_check_accepts_valid_empty_query_results(monkeypatch):
    calls = []
    def get(self, url, headers):
        calls.append(url)
        return {"hits": []}
    monkeypatch.setattr(WebOfScienceAdapter, "_get_json", get)
    assert connect_wos.verify_key("fixture")["authorized"]
    assert len(calls) == 1 and parse_qs(urlsplit(calls[0]).query)["limit"] == ["1"]


def test_key_file_handles_bom_and_preserves_other_env_settings(tmp_path):
    key = tmp_path / "WOS_API_KEY.txt"
    key.write_text('WOS_API_KEY="private-fixture"\n', encoding="utf-8-sig")
    assert connect_wos.read_key(key) == "private-fixture"
    key.write_text("说明文字\nprivate-fixture", encoding="utf-8")
    with pytest.raises(ValueError):
        connect_wos.read_key(key)
    env = tmp_path / ".env"
    env.write_text("# Keep this comment\nSERPAPI_API_KEY=other\nWOS_API_KEY=old\n", encoding="utf-8")
    connect_wos.save_local_key("new", env)
    assert env.read_text() == "# Keep this comment\nSERPAPI_API_KEY=other\nWOS_API_KEY=new\n"


def test_activation_sends_key_via_stdin_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setattr(connect_wos, "ROOT", tmp_path)
    monkeypatch.setattr(connect_wos, "github_cli", lambda: "gh")
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(connect_wos.subprocess, "run", run)
    connect_wos.activate("private-fixture")
    assert len(calls) == 2 and calls[0][1]["input"] == "private-fixture"
    assert all("private-fixture" not in " ".join(args) for args, _ in calls)
    assert calls[1][0][1:4] == ["workflow", "run", "daily.yml"]
    assert "WOS_API_KEY=private-fixture" in (tmp_path / ".env").read_text()


def test_failed_github_save_does_not_change_local_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(connect_wos, "ROOT", tmp_path)
    monkeypatch.setattr(connect_wos, "github_cli", lambda: "gh")
    monkeypatch.setattr(connect_wos.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(RuntimeError):
        connect_wos.activate("private-fixture")
    assert not (tmp_path / ".env").exists()


def test_rejected_key_never_reaches_configuration(monkeypatch):
    monkeypatch.setattr("sys.argv", ["connect_wos", "--activate"])
    monkeypatch.setattr(connect_wos, "read_key", lambda *_: "invalid")
    monkeypatch.setattr(connect_wos, "verify_key", lambda *_: {"authorized": False, "message": "Invalid key"})
    def forbidden(*args):
        raise AssertionError("Do not save an unverified key")
    monkeypatch.setattr(connect_wos, "activate", forbidden)
    with pytest.raises(SystemExit, match="未通过授权验证"):
        connect_wos.main()
