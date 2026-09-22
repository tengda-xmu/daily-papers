from src.settings import load_env


def test_local_env_does_not_replace_actions_secret(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text('SERPAPI_API_KEY="local-test"\nWECHAT_RSS_URLS=https://example.org/rss?a=b\n', encoding="utf-8")
    monkeypatch.setenv("SERPAPI_API_KEY", "actions-test")
    monkeypatch.delenv("WECHAT_RSS_URLS", raising=False)
    load_env(path)
    import os
    assert os.environ["SERPAPI_API_KEY"] == "actions-test"
    assert os.environ["WECHAT_RSS_URLS"] == "https://example.org/rss?a=b"
    assert capsys.readouterr().out == ""
