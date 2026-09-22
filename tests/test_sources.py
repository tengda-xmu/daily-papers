from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter


def test_elsevier_parse():
    records = ElsevierAdapter.parse_payload({"search-results": {"entry": [{"dc:title": "AI maintenance", "prism:doi": "10.1/x", "citedby-count": "4"}]}})
    assert records[0].doi == "10.1/x" and records[0].citation_count == 4


def test_scholar_parse():
    records = GoogleScholarAdapter.parse_payload({"organic_results": [{"result_id": "1", "title": "Design", "publication_info": {"authors": [{"name": "A"}]}}]})
    assert records[0].authors == ["A"]


def test_researchgate_import_formats():
    assert ResearchGateImportAdapter.parse_json([{ "title": "Paper", "doi": "10/a" }])[0].doi == "10/a"
    assert ResearchGateImportAdapter.parse_csv("title,year\nPaper,2024")[0].published_at == "2024"


def test_wechat_rss_parse():
    body = "<rss><channel><item><title>研究文章</title><link>https://x</link><description>摘要</description></item></channel></rss>"
    assert WeChatRSSAdapter.parse(body)[0].landing_url == "https://x"
