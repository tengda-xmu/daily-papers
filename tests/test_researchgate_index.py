import io
import json
from collections import Counter
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from src.models import RawRecord, SourceStatus
from src.pipeline import deduplicate, run_pipeline
from src.sources.researchgate import ResearchGateAdapter, ResearchGateIndexAdapter, publication_url
from src.sources.researchgate_import import ResearchGateImportAdapter
from tools.build_site import source_directory


DATES = datetime(2026, 8, 24), datetime(2026, 9, 23)
LINK = "https://www.researchgate.net/profile/Author/publication/12345_LLM_maintenance/links/abc/paper.pdf?token=private"


@pytest.mark.parametrize("url", [
    "https://researchgate.net.evil.test/publication/12345", "https://evilresearchgate.net/publication/12345",
    "https://user:password@researchgate.net/publication/12345", "https://researchgate.net:444/publication/12345",
    "https://researchgate.net/login", "https://researchgate.net/profile/Author", "https://researchgate.net/publication/no-id",
])
def test_index_rejects_non_publication_and_untrusted_urls(url):
    assert publication_url(url) == ""


def test_index_only_exports_canonical_metadata_and_explicit_provenance():
    payload = {"organic_results": [{
        "title": "LLM maintenance", "link": LINK, "snippet": "Short search excerpt",
        "publication_info": {"summary": "A - Nature Machine Intelligence, 2026 - researchgate.net", "authors": [{"name": "A"}]},
        "api_key": "secret", "cookies": "session", "resources": [{"link": LINK}],
    }, {"title": "Unrelated", "link": "https://example.org/no-rg"},
        {"title": "Indexed alternative", "link": "https://doi.org/10.1234/alternative", "resources": [{"link": LINK}]},
        {"title": "Future", "link": "https://researchgate.net/publication/777_Future", "publication_info": {"year": 2027}}]}
    records = ResearchGateIndexAdapter.parse_payload(payload)
    assert len(records) == 3
    record = records[0]
    assert record.landing_url == "https://www.researchgate.net/publication/12345_LLM_maintenance"
    assert record.source_id == "12345" and record.oa_url == ""
    assert record.venue == "Nature Machine Intelligence" and record.published_at == "2026"
    assert record.raw_metadata["provider"] == "Google Scholar via SerpApi"
    assert record.raw_metadata["abstract_kind"] == "search_snippet"
    assert records[1].raw_metadata["date_precision"] == "unknown" and records[1].published_at == ""
    assert records[1].doi == "10.1234/alternative"
    assert not any(word in json.dumps(record.to_dict()) for word in ("private", "secret", "session", ".pdf"))


def test_index_single_paid_query_date_scope_and_cache(tmp_path, monkeypatch):
    calls = []
    def response(request, timeout):
        params = parse_qs(urlsplit(request.full_url).query)
        calls.append(params)
        assert urlsplit(request.full_url).hostname == "serpapi.com"
        return io.BytesIO(json.dumps({"api_key": "fixture-secret", "organic_results": [
            {"title": "LLM maintenance", "link": LINK, "publication_info": {"year": 2026}},
            {"title": "Future", "link": "https://researchgate.net/publication/999_Future", "publication_info": {"year": 2027}},
        ]}).encode())
    monkeypatch.setattr("src.sources.google_scholar.urlopen", response)
    adapter = ResearchGateIndexAdapter(api_key="fixture-secret", cache_dir=tmp_path)
    assert len(adapter.fetch(*DATES)) == 1
    assert len(adapter.fetch(*DATES)) == 1 and len(calls) == 1
    assert len(adapter.queries) == 1 and calls[0]["q"][0].startswith("site:researchgate.net ")
    assert calls[0]["as_ylo"] == calls[0]["as_yhi"] == ["2026"]
    assert all("fixture-secret" not in p.read_text() for p in tmp_path.glob("*.json"))


def test_successful_empty_search_is_cached_but_quota_errors_are_not(tmp_path, monkeypatch):
    calls = []
    def response(request, timeout):
        calls.append(request)
        return io.BytesIO(json.dumps({"search_metadata": {"status": "Success"},
                                     "error": "Google hasn't returned any results for this query."}).encode())
    monkeypatch.setattr("src.sources.google_scholar.urlopen", response)
    adapter = ResearchGateIndexAdapter(api_key="fixture-secret", cache_dir=tmp_path)
    assert adapter.fetch(*DATES) == [] and adapter.status.status == "no_data"
    assert adapter.fetch(*DATES) == [] and len(calls) == 1
    def denied(request, timeout):
        raise HTTPError(request.full_url, 429, "fixture-secret", {}, None)
    monkeypatch.setattr("src.sources.google_scholar.urlopen", denied)
    adapter = ResearchGateIndexAdapter(api_key="fixture-secret", cache_dir=tmp_path / "quota")
    assert adapter.fetch(*DATES) == [] and adapter.status.status == "quota_exhausted"
    assert "fixture-secret" not in adapter.status.message and not list((tmp_path / "quota").glob("*.json"))


class StubSource:
    name = "ResearchGate"
    def __init__(self, records=(), state="ok"):
        self.records = list(records)
        self.status = SourceStatus(self.name, state, len(records), "fixture status")
        self.calls = 0
    def fetch(self, *dates):
        self.calls += 1
        return self.records


def test_fresh_browser_import_avoids_paid_fallback(tmp_path):
    path = tmp_path / "researchgate.json"
    path.write_text(json.dumps({"exported_at": "2026-09-22T12:00:00Z", "records": [
        {"title": "LLM maintenance", "landing_url": publication_url(LINK), "published_at": "2026-09-22"}
    ]}), encoding="utf-8")
    index = StubSource()
    adapter = ResearchGateAdapter(importer=ResearchGateImportAdapter([str(path)]), index=index)
    assert len(adapter.fetch(*DATES)) == 1 and index.calls == 0
    assert adapter.status.message.startswith("本地连接器已导入")


def test_missing_export_uses_honestly_labelled_index_and_can_disable_it():
    record = RawRecord("ResearchGate", "12345", "LLM maintenance", landing_url=publication_url(LINK))
    importer, index = StubSource(state="configuration_missing"), StubSource([record])
    adapter = ResearchGateAdapter(importer=importer, index=index)
    assert adapter.fetch(*DATES) == [record] and index.calls == 1
    assert adapter.status.status == "ok" and adapter.status.message.startswith("公开索引已接入")
    html = source_directory({"ResearchGate": adapter.status.to_dict()}, Counter())
    assert "公开索引已接入" in html and "Google Scholar / SerpApi" in html
    adapter = ResearchGateAdapter(importer=importer, index=index, public_index=False)
    assert adapter.fetch(*DATES) == [] and index.calls == 1
    assert adapter.status.status == "configuration_missing"


def test_index_failure_preserves_stale_local_data_and_other_sources(tmp_path):
    record = RawRecord("ResearchGate", "1", "LLM predictive maintenance", abstract="fault diagnosis", published_at="2026")
    adapter = ResearchGateAdapter(importer=StubSource([record], "partial"), index=StubSource(state="quota_exhausted"))
    assert adapter.fetch(*DATES) == [record] and adapter.status.status == "partial"
    assert "quota_exhausted" in adapter.status.message
    failed = ResearchGateAdapter(importer=StubSource(state="configuration_missing"), index=StubSource(state="quota_exhausted"))
    other = StubSource([RawRecord("Elsevier", "2", "LLM predictive maintenance", abstract="fault diagnosis")])
    other.name = "Elsevier"
    other.status = SourceStatus("Elsevier", "ok", 1)
    payload = run_pipeline(adapters=[failed, other], since=DATES[0], until=DATES[1], output_path=tmp_path / "daily.json")
    assert payload["source_status"]["ResearchGate"]["status"] == "quota_exhausted"
    assert payload["source_status"]["Elsevier"]["count"] == 1


def test_index_snippets_do_not_replace_verified_paper_abstracts():
    verified = RawRecord("Elsevier", "1", "LLM maintenance", doi="10.1234/paper", abstract="Verified engineering abstract")
    indexed = RawRecord("ResearchGate", "2", verified.title, doi=verified.doi, abstract="Longer truncated search excerpt " * 20,
                        raw_metadata={"abstract_kind": "search_snippet", "sources": ["ResearchGate", "Google Scholar"]})
    merged = deduplicate([indexed, verified])[0]
    assert merged.source == "Elsevier" and merged.abstract == "Verified engineering abstract"
    assert "abstract_kind" not in merged.raw_metadata
    assert merged.raw_metadata["sources"] == ["Elsevier", "Google Scholar", "ResearchGate"]
