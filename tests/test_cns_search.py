from datetime import datetime
from urllib.parse import urlsplit, parse_qs

from src.sources.cns_journals import CNSJournalAdapter


def test_cns_search_scopes_journal_date_and_rejects_wrong_venue(monkeypatch):
    config = {"lookback_days": 180, "query": "structural reliability", "journals": [{"name": "Nature Communications", "issn": "2041-1723"}]}
    a = CNSJournalAdapter(config=config)
    calls = []
    def response(url, headers):
        calls.append(url)
        return {"message": {"items": [
            {"title": ["Relevant"], "DOI": "10/a", "container-title": ["Nature Communications"], "published": {"date-parts": [[2026, 8, 1]]}},
            {"title": ["Wrong journal"], "DOI": "10/b", "container-title": ["Other"], "published": {"date-parts": [[2026, 8, 1]]}},
            {"title": ["Future"], "DOI": "10/c", "container-title": ["Nature Communications"], "published": {"date-parts": [[2027, 8, 1]]}},
        ]}}
    monkeypatch.delenv("CNS_LOOKBACK_DAYS", raising=False)
    monkeypatch.setattr(a, "_get_json", response)
    rows = a.fetch(datetime(2026, 8, 1), datetime(2026, 9, 23))
    assert len(rows) == 1 and rows[0].raw_metadata["query_scope"] == a.name
    assert "issn:2041-1723" in parse_qs(urlsplit(calls[0]).query)["filter"][0]
    assert "from-pub-date:2026-03-27" in calls[0].replace("%3A", ":")
    assert a.status.status == "ok"


def test_partial_cns_failure_keeps_other_journal_results(monkeypatch):
    from urllib.error import HTTPError
    config = {"lookback_days": 180, "query": "structural", "journals": [
        {"name": "Nature Communications", "issn": "2041-1723"},
        {"name": "Science Advances", "issn": "2375-2548"}]}
    a = CNSJournalAdapter(config=config)
    def respond(url, headers):
        if "2375-2548" in url:
            raise HTTPError(url, 503, "unavailable", {}, None)
        return {"message": {"items": [{"title": ["Machine learning structural reliability"], "DOI": "10/a",
                                       "container-title": ["Nature Communications"], "published": {"date-parts": [[2026, 8, 1]]}}]}}
    monkeypatch.delenv("CNS_LOOKBACK_DAYS", raising=False)
    monkeypatch.setattr(a, "_get_json", respond)
    monkeypatch.setattr("src.sources.cns_journals.time.sleep", lambda _: None)
    records = a.fetch(datetime(2026, 8, 1), datetime(2026, 9, 23))
    assert len(records) == 1 and a.status.status == "partial"
    assert "1/2 journals" in a.status.message and "HTTP 503" in a.status.message
