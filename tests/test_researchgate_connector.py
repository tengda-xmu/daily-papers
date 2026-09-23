import json
from datetime import datetime, timezone

import pytest

from connectors.researchgate_sync.export import normalize_records, publication_date, researchgate_url, save_export
from src.models import in_date_window
from src.sources.researchgate_import import ResearchGateImportAdapter
from tools.publish_researchgate import public_export


def test_urls_are_limited_to_public_researchgate_pages():
    assert researchgate_url("https://www.researchgate.net/profile/Da-Teng-6?tracking=private") == "https://www.researchgate.net/profile/Da-Teng-6"
    for url in ("https://www.researchgate.net/messages", "https://www.researchgate.net/login",
                "https://researchgate.net.attacker.test/profile/Someone", "http://www.researchgate.net/publication/123",
                "https://secret@www.researchgate.net/publication/123"):
        with pytest.raises(ValueError):
            researchgate_url(url)


def test_profile_and_publication_metadata_merge_without_query_or_private_fields():
    rows = [{"title": "Neural models for structural reliability", "authors": ["A Person"],
             "published_at": "May 2026", "landing_url": "https://www.researchgate.net/publication/123_Neural?token=private",
             "text": "DOI: 10.1000/example", "cookies": "private"},
            {"title": "Neural models for structural reliability", "authors": [],
             "landing_url": "https://www.researchgate.net/publication/123_Neural"},
            {"title": "Download full-text", "landing_url": "https://www.researchgate.net/publication/123_Neural"}]
    clean = normalize_records(rows)
    assert len(clean) == 1
    assert clean[0]["published_at"] == "2026-05"
    assert clean[0]["doi"] == "10.1000/example"
    assert clean[0]["authors"] == ["A Person"]
    assert "private" not in json.dumps(clean)


def test_month_precision_does_not_promote_old_articles_into_current_month():
    since = datetime(2026, 8, 24, tzinfo=timezone.utc)
    until = datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert publication_date("September 2026") == "2026-09"
    assert publication_date("2026/9/2") == "2026-09-02"
    assert publication_date("2026-99-01") == ""
    assert not in_date_window(publication_date("May 2026"), since, until)
    assert in_date_window("2026-08", since, until)
    assert in_date_window("2026-09", since, until)
    assert not in_date_window("2026-10", since, until)


def test_import_reports_received_and_current_counts_and_stale_export(tmp_path):
    payload = {"exported_at": "2026-09-23T01:00:00Z", "failed_pages": 0,
               "session": "private", "records": [{"title": "AI structural reliability", "published_at": "2026-05",
                   "landing_url": "https://www.researchgate.net/publication/123_paper"}]}
    clean = public_export(payload)
    assert "private" not in json.dumps(clean)
    assert clean["exported_at"] == "2026-09-23T01:00:00+00:00"
    path = tmp_path / "researchgate.json"
    path.write_text(json.dumps(clean), encoding="utf-8")
    adapter = ResearchGateImportAdapter([str(path)])
    since = datetime(2026, 8, 24, tzinfo=timezone.utc)
    until = datetime(2026, 9, 23, 2, tzinfo=timezone.utc)
    assert adapter.fetch(since, until) == []
    assert adapter.status.status == "no_data"
    assert "已导入 1 条" in adapter.status.message
    assert "符合日期范围 0 条" in adapter.status.message
    adapter.fetch(since, datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert adapter.status.status == "partial"
    assert "超过 3 天" in adapter.status.message


def test_empty_or_login_results_preserve_previous_export(tmp_path):
    output = tmp_path / "researchgate.json"
    previous = '{"records": [{"title": "previous export"}]}'
    output.write_text(previous, encoding="utf-8")
    for rows in ([], [{"title": "Access restricted", "landing_url": "https://www.researchgate.net/login"}]):
        with pytest.raises(ValueError):
            save_export(rows, output)
        assert output.read_text(encoding="utf-8") == previous
