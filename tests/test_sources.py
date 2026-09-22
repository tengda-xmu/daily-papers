from datetime import datetime

from src.sources.elsevier import ElsevierAdapter
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.public_literature import (
    ArxivAdapter, CrossrefAdapter, OpenAlexAdapter, PubMedAdapter,
    SemanticScholarAdapter, WebOfScienceAdapter,
)


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


def test_public_source_parsers():
    arxiv = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1</id><title>Paper</title><summary>Abstract</summary><published>2026-09-21T00:00:00Z</published><author><name>Author</name></author></entry></feed>'
    assert ArxivAdapter.parse_xml(arxiv)[0].authors == ["Author"]
    assert OpenAlexAdapter.parse_payload({"results": [{"id": "w1", "title": "Open", "abstract_inverted_index": {"paper": [1], "A": [0]}, "authorships": []}]})[0].abstract == "A paper"
    assert CrossrefAdapter.parse_payload({"message": {"items": [{"DOI": "10/x", "title": ["Cross"], "container-title": ["Journal"]}]}})[0].doi == "10/x"
    assert SemanticScholarAdapter.parse_payload({"data": [{"paperId": "p1", "title": "Semantic", "authors": [{"name": "A"}], "year": 2026}]})[0].published_at == "2026"
    assert PubMedAdapter.parse_xml('<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>1</PMID><Article><ArticleTitle>PubMed paper</ArticleTitle><Journal><Title>Journal</Title></Journal><Abstract><AbstractText>Abstract</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>')[0].title == "PubMed paper"
    adapter = WebOfScienceAdapter(api_key="")
    adapter.fetch(datetime(2026, 1, 1), datetime(2026, 1, 2))
    assert adapter.status.status == "authorization_required"
