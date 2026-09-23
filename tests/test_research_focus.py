from copy import deepcopy
from datetime import datetime, timezone

from src.models import RawRecord, SourceStatus
from src.pipeline import rank, run_pipeline
from src.reading_notes import cached_analysis, curated_entries, valid_analysis
from src.research_focus import focus_tags


def test_focus_recognizes_plurals_and_hyphens_without_marketing_false_positives():
    assert focus_tags("Multi-agent LLMs for design") == ["大模型", "智能体"]
    assert focus_tags("Large-language-model fault diagnosis") == ["大模型"]
    assert focus_tags("Agentic scientific computing") == ["智能体"]
    assert focus_tags("Multi-agent reinforcement learning") == ["智能体"]
    assert focus_tags("LLM-driven constitutive models: two agents are better than one") == ["大模型", "智能体"]
    assert focus_tags("Surrogate prediction of material fatigue") == []
    assert focus_tags("Travel agents and marketing campaigns") == []


def test_journal_priority_is_preserved_and_focus_wins_within_journals():
    records = [RawRecord("test", "general", "Machine learning for structural fatigue", venue="Nature Communications"),
               RawRecord("test", "llm", "Large language models for structural fatigue", venue="npj Computational Materials"),
               RawRecord("test", "agent", "Multi-agent LLMs for constitutive models", venue="npj Artificial Intelligence"),
               RawRecord("test", "main", "Agentic LLMs for structural fatigue", venue="Nature")]
    assert [r.source_id for r in rank(records)] == ["agent", "llm", "general", "main"]


def test_irrelevant_llms_do_not_enter_the_recommendation_pool():
    records = [RawRecord("test", "marketing", "LLM agents for marketing campaigns"),
               RawRecord("test", "network", "Structural analysis of language model neural networks"),
               RawRecord("test", "mechanics", "Multi-agent LLMs for physics-constrained constitutive models")]
    assert [r.source_id for r in rank(records)] == ["mechanics"]


def test_scholar_excerpt_preserves_verified_metadata_and_reading_notes():
    paper = next(row["paper"] for row in curated_entries()
                 if row["paper"]["doi"] == "10.48550/arxiv.2608.07978")
    verified = RawRecord.from_mapping(paper)
    snippet = (
        "… Trustworthiness is the key to bringing LLM-driven structural design into practice. "
        "This paper proposes a closedloop multi-agent framework whose feedback comes not from the model’s …"
    )
    assert len(snippet) > len(verified.abstract)
    scholar = RawRecord(
        "Google Scholar", "scholar-id", verified.title,
        authors=["J Luo", "W Lin"], abstract=snippet, published_at="2026",
        venue="J Luo et al. - arXiv preprint, 2026 - arxiv.org",
        landing_url="https://arxiv.org/abs/2608.07978",
        oa_url="https://arxiv.org/pdf/2608.07978", source_score=.75,
    )
    result = rank([scholar, verified])
    assert len(result) == 1
    merged = result[0]
    assert merged.abstract == paper["abstract"]
    assert merged.authors == paper["authors"]
    assert merged.published_at == paper["published_at"]
    assert merged.venue == paper["venue"]
    assert merged.topic_tags == ["generative_design"]
    assert merged.raw_metadata["sources"] == ["Google Scholar", "arXiv"]
    assert valid_analysis(cached_analysis(merged))


def test_model_budget_covers_extended_and_does_not_retry_cached_notes(monkeypatch):
    sample = deepcopy(curated_entries()[0]["analysis"])
    calls, cache = [], {}
    class Fixture:
        name = "fixture"
        status = SourceStatus(name, "ok", 12)
        def fetch(self, since, until):
            return [RawRecord("fixture", str(i), f"Large language model for structural fatigue case {i}",
                              abstract="An independent evaluation of machine learning and physical modeling for structural fatigue, including uncertainty and held-out loading conditions.",
                              doi=f"10.fixture/{i}", landing_url=f"https://example.org/paper/{i}") for i in range(12)]
    def summarize(record):
        calls.append(record.doi)
        cache[record.doi] = deepcopy(sample)
        return cache[record.doi]
    monkeypatch.setenv("LLM_API_KEY", "fixture-key")
    monkeypatch.setenv("MAX_CORE", "5")
    monkeypatch.setenv("MAX_EXTENDED", "5")
    monkeypatch.setattr("src.pipeline.cached_analysis", lambda record: cache.get(record.doi))
    monkeypatch.setattr("src.pipeline._llm_summary", summarize)
    args = {"adapters": [Fixture()], "until": datetime(2026, 9, 23, tzinfo=timezone.utc)}
    result = run_pipeline(**args)
    assert len(result["core"]) == len(result["extended"]) == 5
    assert len(calls) == result["analysis_status"]["llm_attempts"] == 10
    run_pipeline(**args)
    assert len(calls) == 10  # Both tiers reuse validated notes.
