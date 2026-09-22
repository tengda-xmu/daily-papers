from src.models import RawRecord
from src.pipeline import deduplicate, fallback_summary, rank


def test_dedupe_and_rank():
    a = RawRecord("Elsevier", "1", "LLM predictive maintenance", doi="10/a", abstract="agent")
    b = RawRecord("ResearchGate", "2", "LLM predictive maintenance", doi="10/a")
    result = rank(deduplicate([a, b]))
    assert len(result) == 1 and "ai_maintenance" in result[0].topic_tags


def test_fallback_without_abstract():
    assert "title" in fallback_summary(RawRecord("x", "1", "title"))["summary"]
