import json
from datetime import datetime, timezone
from pathlib import Path

from src.models import RawRecord
from src.pipeline import deduplicate, fallback_summary, rank, run_pipeline
from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.public_literature import ArxivAdapter, OpenAlexAdapter, CrossrefAdapter, SemanticScholarAdapter, PubMedAdapter, WebOfScienceAdapter
from src.venues import classify_venue
from src.catalog import paper_facets


FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_parsers():
    assert ElsevierAdapter.parse_payload(json.loads((FIXTURES / "elsevier.json").read_text(encoding="utf-8")))[0].doi == "10.1000/example"
    assert GoogleScholarAdapter.parse_payload(json.loads((FIXTURES / "scholar.json").read_text(encoding="utf-8")))[0].citation_count == 7
    assert ResearchGateImportAdapter.parse_json(json.loads((FIXTURES / "researchgate.json").read_text(encoding="utf-8")))[0].doi == "10.1000/fatigue"
    assert WeChatRSSAdapter.parse((FIXTURES / "wechat.xml").read_text(encoding="utf-8"))[0].landing_url.endswith("/1")


def test_duplicate_keeps_richer_record_and_tracks_sources():
    sparse = RawRecord("ResearchGate", "1", "A paper", doi="10/example")
    rich = RawRecord("Elsevier", "2", "A paper", doi="10/example", abstract="A useful abstract", source_score=.9)
    result = deduplicate([sparse, rich])
    assert len(result) == 1
    assert result[0].abstract == "A useful abstract"
    assert "ResearchGate" in result[0].raw_metadata["sources"]


def test_run_pipeline_writes_publishable_payload(tmp_path):
    class Adapter:
        name = "fixture"
        status = type("Status", (), {"source": "fixture", "to_dict": lambda self: {"source": "fixture", "status": "ok", "count": 1, "message": ""}})()

        def fetch(self, since, until):
            return [RawRecord("fixture", "1", "LLM maintenance", abstract="An agent diagnosis study")]

    output = tmp_path / "daily.json"
    payload = run_pipeline(
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 30, tzinfo=timezone.utc),
        adapters=[Adapter()],
        output_path=output,
    )
    assert payload["core"][0]["topic_tags"] == ["ai_maintenance"]
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["papers"][0]["summary"]


def test_fallback_summary_without_abstract_mentions_title():
    assert "title" in fallback_summary(RawRecord("x", "1", "title"))["summary"]


def test_cns_venue_groups():
    assert classify_venue("Nature") == "CNS 正刊"
    assert classify_venue("Science Advances") == "CNS 子刊"
    assert classify_venue("Journal of Structural Engineering") == "ASCE 工程期刊"


def test_catalog_facets_keep_source_and_platform_filters():
    facets = paper_facets({
        "source": "Elsevier", "venue": "Engineering Structures",
        "landing_url": "https://www.sciencedirect.com/science/article/pii/example",
        "topic_tags": ["generative_design"],
    })
    assert "Elsevier" in facets["source_ids"]
    assert "ScienceDirect" in facets["source_ids"]
    assert facets["venue_group"] == "Elsevier 工程与材料期刊"


def test_pipeline_builds_public_adapters_without_planned_sources():
    names = [adapter.name for adapter in __import__("src.pipeline", fromlist=["build_adapters"]).build_adapters()]
    assert names == ["Elsevier", "Google Scholar", "ResearchGate", "微信公众号", "arXiv", "OpenAlex", "Crossref", "Semantic Scholar", "PubMed", "Web of Science"]


def test_irrelevant_ai_and_biological_design_are_not_recommended():
    records = [RawRecord("fixture", "1", "Average results in marketing", abstract="An agent for chatbot usage"),
               RawRecord("fixture", "2", "Generative protein structure design", abstract="Structural optimization for amyloid"),
               RawRecord("fixture", "3", "Neural network for bearing fault diagnosis")]
    assert [r.source_id for r in rank(records)] == ["3"]


def test_empty_adapter_list_does_not_call_live_sources(monkeypatch):
    def unexpected():
        raise AssertionError("Should not build network adapters")
    monkeypatch.setattr("src.pipeline.build_adapters", unexpected)
    assert run_pipeline(adapters=[])["papers"] == []


def test_llm_empty_optional_settings_use_defaults_and_invalid_url_falls_back(monkeypatch):
    import io
    import src.pipeline as pipeline
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    calls = []
    def respond(request, timeout):
        calls.append(request)
        result = {key: "Evidence" for key in ("summary", "method", "recommendation", "problem", "findings", "limitations", "connection")}
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode())
    monkeypatch.setattr(pipeline, "urlopen", respond)
    paper = RawRecord("fixture", "1", "AI maintenance")
    assert pipeline.summarize(paper)["summary"] == "Evidence"
    assert calls[0].full_url == "https://api.openai.com/v1/chat/completions"
    assert json.loads(calls[0].data)["model"]
    monkeypatch.setenv("LLM_BASE_URL", "invalid")
    assert pipeline.summarize(paper) == fallback_summary(paper)
