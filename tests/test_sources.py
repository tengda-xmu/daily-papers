from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter


def test_elsevier_parse():
    records = ElsevierAdapter.parse_payload({"search-results": {"entry": [{"dc:title": "AI maintenance", "prism:doi": "10.1/x", "citedby-count": "4"}]}})
    assert records[0].doi == "10.1/x" and records[0].citation_count == 4


def test_elsevier_xml_parse():
    body = """<search-results xmlns:dc=\"http://purl.org/dc/elements/1.1/\" xmlns:prism=\"http://prismstandard.org/namespaces/basic/2.0/\"><entry><dc:title>XML paper</dc:title><prism:doi>10.1/xml</prism:doi><prism:coverDate>2026-09-22</prism:coverDate></entry></search-results>"""
    assert ElsevierAdapter.parse_xml(body)[0].doi == "10.1/xml"


def test_scholar_parse():
    records = GoogleScholarAdapter.parse_payload({"organic_results": [{"result_id": "1", "title": "Design", "publication_info": {"authors": [{"name": "A"}]}}]})
    assert records[0].authors == ["A"]


def test_scholar_extracts_year_and_doi():
    records = GoogleScholarAdapter.parse_payload({"organic_results": [{"result_id": "1", "title": "Design", "link": "https://doi.org/10.1234/design", "publication_info": {"summary": "A - 2026 - Journal"}}]})
    assert records[0].published_at == "2026" and records[0].doi == "10.1234/design"


def test_researchgate_import_formats():
    assert ResearchGateImportAdapter.parse_json([{ "title": "Paper", "doi": "10/a" }])[0].doi == "10/a"
    assert ResearchGateImportAdapter.parse_csv("title,year\nPaper,2024")[0].published_at == "2024"
    assert ResearchGateImportAdapter.parse_bibtex("@article{x, title={Paper}, author={Doe, Jane and Smith, John}, year={2024}} ")[0].authors == ["Doe, Jane", "Smith, John"]


def test_wechat_rss_parse():
    body = "<rss><channel><item><title>研究文章</title><link>https://x</link><description>摘要</description></item></channel></rss>"
    assert WeChatRSSAdapter.parse(body)[0].landing_url == "https://x"
