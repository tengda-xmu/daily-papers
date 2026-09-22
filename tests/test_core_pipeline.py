import json
from datetime import datetime, timezone
from pathlib import Path

from src.models import RawRecord
from src.pipeline import deduplicate, fallback_summary, rank, run_pipeline
from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
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
