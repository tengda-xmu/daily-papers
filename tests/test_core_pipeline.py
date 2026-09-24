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


def test_deduplicate_versions_and_cross_source_aliases_without_merging_other_authors():
    rows = [RawRecord("OpenAlex", "1", "Fatigue models", authors=["Author A"], doi="10.1000/v1"),
            RawRecord("Crossref", "2", "Fatigue models", authors=["Author A"], doi="10.1000/v2"),
            RawRecord("Elsevier", "3", "Updated fatigue models", authors=["Author A"], doi="10.1000/v2", abstract="Details"),
            RawRecord("OpenAlex", "4", "Fatigue models", authors=["Author B"], doi="10.1000/other"),
            RawRecord("arXiv", "https://arxiv.org/abs/2609.12345v1", "Old title"),
            RawRecord("ResearchGate", "6", "New title", oa_url="https://arxiv.org/pdf/2609.12345v2", abstract="Metadata")]
    merged = deduplicate(rows)
    assert len(merged) == 3
    assert merged[0].abstract == "Details"
    assert set(merged[0].raw_metadata["sources"]) == {"OpenAlex", "Crossref", "Elsevier"}
    assert merged[1].authors == ["Author B"]
    assert set(merged[2].raw_metadata["sources"]) == {"arXiv", "ResearchGate"}


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
    assert payload["core"] == []  # Unreviewed records cannot fill a core slot.
    assert payload["extended"] == []  # Extended reading has the same quality gate.
    assert payload["papers"][0]["topic_tags"] == ["ai_maintenance"]
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
    assert names == ["CNS 子刊专项", "Elsevier", "Google Scholar", "ResearchGate", "微信公众号", "arXiv", "OpenAlex", "Crossref", "Semantic Scholar", "PubMed", "Web of Science"]


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


def test_llm_empty_optional_settings_use_defaults_and_invalid_url_falls_back(monkeypatch, tmp_path):
    import io
    import src.pipeline as pipeline
    from src.reading_notes import NOTE_FIELDS
    monkeypatch.setattr("src.reading_notes.CACHE", tmp_path)
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "")
    calls = []
    def respond(request, timeout):
        calls.append(request)
        result = {"title_zh": "基于监测证据的结构可靠性分析", "summary": "本研究关注利用监测数据分析结构响应及其不确定性，并明确区分论文结果与模型推断，应用时还需验证不同工况下的适用范围。",
                  "recommendation": "适用于研究结构监测与可靠性，并对照原文核查适用条件。",
                  "deep_read": {key: key + "：" + "这项分析依据公开摘要讨论结构响应与模型不确定性，具体数据、试验设置和跨工况泛化能力仍需结合论文原文进一步核查。" for key in NOTE_FIELDS}}
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode())
    monkeypatch.setattr(pipeline, "urlopen", respond)
    paper = RawRecord("fixture", "1", "AI maintenance", abstract="An engineering study of machine learning and structural reliability with monitoring evidence and explicit uncertainties.", landing_url="https://example.org/paper")
    result = pipeline.summarize(paper)
    assert result["analysis_status"] == "ready" and "method" in result["deep_read"]
    assert calls[0].full_url == "https://api.openai.com/v1/chat/completions"
    assert json.loads(calls[0].data)["model"]
    monkeypatch.setenv("LLM_BASE_URL", "invalid")
    paper.abstract += " Different evidence invalidates the automatic cache."
    assert pipeline.summarize(paper) == fallback_summary(paper)


def test_cns_family_precedes_main_journals_and_more_topic_matches():
    records = [RawRecord("fixture", "multi", "Machine learning structural reliability and generative design for predictive maintenance", venue="Engineering Structures"),
               RawRecord("fixture", "main", "Neural network for bearing fault diagnosis", venue="Nature"),
               RawRecord("fixture", "sub", "Neural network for bearing fault diagnosis", authors=["Different author"], venue="Nature Communications")]
    assert [r.source_id for r in rank(records)] == ["sub", "main", "multi"]


def test_curated_core_notes_are_detailed_chinese_and_match_real_papers():
    from src.reading_notes import curated_entries, curated_records, valid_analysis
    entries = curated_entries()
    assert len(entries) >= 10
    assert all(valid_analysis(row["analysis"]) for row in entries)
    assert sum(classify_venue(row["paper"]["venue"]) == "CNS 子刊" for row in entries) >= 8
    assert len(curated_records(datetime(2026, 9, 23, tzinfo=timezone.utc), 180)) >= 10
    assert curated_records(datetime(2027, 9, 23, tzinfo=timezone.utc), 180) == []


def test_core_keeps_complete_chinese_notes_when_live_source_fails(monkeypatch, tmp_path):
    from src.reading_notes import NOTE_FIELDS, valid_analysis
    from tools.build_site import render
    class Unavailable:
        name = "fixture failure"
        def fetch(self, since, until):
            raise TimeoutError("fixture timeout")
    monkeypatch.setattr("src.pipeline.build_adapters", lambda: [Unavailable()])
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("CNS_LOOKBACK_DAYS", "180")
    monkeypatch.setenv("MAX_CORE", "5")
    monkeypatch.setenv("MAX_EXTENDED", "5")
    result = run_pipeline(until=datetime(2026, 9, 23, tzinfo=timezone.utc), output_path=tmp_path / "daily.json")
    assert result["source_status"]["fixture failure"]["status"] == "error"
    assert len(result["core"]) == 5 and all(valid_analysis(p) for p in result["core"])
    assert len(result["extended"]) == 5 and all(valid_analysis(p) for p in result["extended"])
    # The first pass now covers all configured directions, including fatigue
    # studies without an LLM; focus still breaks ties within each direction.
    assert len({p['recommended_direction'] for p in result['core'][:3]}) == 3
    assert not ({p["id"] for p in result["core"]} & {p["id"] for p in result["extended"]})
    assert all(p["venue_group"] == "CNS 子刊" for p in result["core"])
    assert {tag for p in result["core"] for tag in p["topic_tags"]} == {"ai_maintenance", "generative_design", "fatigue_reliability"}
    assert result["analysis_status"]["llm_attempts"] == 0
    rendered = render(result)
    assert rendered.count('class="reading-notes"') == 10
    assert rendered.count('class="analysis-provenance"') == 10
    assert rendered.count('class="original-title"') == 10
    assert rendered.count('class="publication-type"') == 2
    assert all(len(p["deep_read"]) == len(NOTE_FIELDS) for p in result["core"])


def test_invalid_analysis_cannot_be_promoted_to_core():
    from copy import deepcopy
    from src.reading_notes import curated_entries, valid_analysis
    sample = curated_entries()[0]["analysis"]
    for field, value in [("deep_read", []), ("deep_read", "malformed"),
                         ("summary", "English abstract only"), ("analysis_sources", ["javascript:alert(1)"]),
                         ("recommendation", ""), ("analysis_sources", "https://example.org/paper")]:
        malformed = deepcopy(sample)
        malformed[field] = value
        assert not valid_analysis(malformed)
