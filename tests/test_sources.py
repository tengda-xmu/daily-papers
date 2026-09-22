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


def test_wechat_keeps_short_public_metadata_without_feed_credentials(monkeypatch):
    import json
    rows = WeChatRSSAdapter.parse(json.dumps({"items": [{
        "title": "Paper", "url": "https://mp.weixin.qq.com/s/public", "summary": "<p>" + "x" * 900 + "</p>",
        "cookies": "private-session", "content": "private-fulltext",
    }]}), "https://rss.example/feed?token=private-token")
    output = json.dumps(rows[0].to_dict())
    assert "private-" not in output and len(rows[0].abstract) <= 301
    monkeypatch.setenv("WECHAT_RSS_HEADERS", "[]")
    adapter = WeChatRSSAdapter(urls=["https://rss.example/"])
    assert adapter.fetch(datetime(2026, 1, 1), datetime(2026, 12, 31)) == []
    assert adapter.status.status == "configuration_missing"


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


def test_arxiv_uses_official_rss_when_search_fails(monkeypatch):
    from urllib.error import HTTPError
    from datetime import timezone
    calls = []
    def response(url, headers=None):
        calls.append(url)
        if "export.arxiv.org" in url:
            raise HTTPError(url, 406, "Unavailable", {}, None)
        return '''<rss><channel><item><title>AI maintenance</title><guid>2609.1</guid>
          <link>https://arxiv.org/abs/2609.1</link><description>Abstract: Data</description>
          <pubDate>Tue, 22 Sep 2026 00:00:00 -0400</pubDate></item></channel></rss>'''
    adapter = ArxivAdapter()
    monkeypatch.setattr(adapter, "_get_text", response)
    records = adapter.fetch(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc))
    assert adapter.status.status == "ok" and "RSS fallback" in adapter.status.message
    assert records[0].source == "arXiv" and records[0].raw_metadata["method"] == "rss"
    assert calls[-1].startswith("https://rss.arxiv.org/")


def test_semantic_bulk_bounds_dates_and_recovers_from_one_rate_limit(monkeypatch):
    from urllib.error import HTTPError
    from urllib.parse import urlsplit, parse_qs
    from datetime import timezone
    import src.sources.public_literature as sources
    requests, sleeps = [], []
    def response(url, headers=None):
        requests.append(url)
        if len(requests) == 1:
            raise HTTPError(url, 429, "rate limit", {"Retry-After": "6"}, None)
        return {"data": [{"paperId": "1", "title": "AI fatigue", "publicationDate": "2026-09-21"},
                          {"paperId": "2", "title": "Future paper", "publicationDate": "2027-01-01"}]}
    a = SemanticScholarAdapter()
    monkeypatch.setattr(a, "_get_json", response)
    monkeypatch.setattr(sources.time, "sleep", sleeps.append)
    rows = a.fetch(datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc))
    query = parse_qs(urlsplit(requests[0]).query)
    assert "/paper/search/bulk?" in requests[0]
    assert query["publicationDateOrYear"] == ["2026-09-20:2026-09-23"]
    assert len(rows) == 1 and sleeps[0] == 6


def test_wos_maps_document_schema():
    rows = WebOfScienceAdapter.parse_payload({"hits": [{
        "uid": "WOS:1", "title": "Structural fatigue",
        "source": {"sourceTitle": "International Journal of Fatigue", "publishYear": 2026},
        "names": {"authors": [{"displayName": "Author"}]}, "identifiers": {"doi": "10.1000/test"},
        "citations": [{"db": "WOS", "count": 3}], "links": {"record": "https://www.webofscience.com/1"},
    }]})
    assert rows[0].doi == "10.1000/test" and rows[0].authors == ["Author"]
    assert rows[0].citation_count == 3 and rows[0].published_at == "2026"


def test_crossref_and_pubmed_keep_date_and_nested_text():
    from datetime import timezone
    from src.models import in_date_window
    row = CrossrefAdapter.parse_payload({"message": {"items": [{"title": ["Future"], "published": {"date-parts": [[2027, 1, 2]]}}]}})[0]
    assert row.published_at == "2027-01-02"
    assert not in_date_window(row.published_at, datetime(2026, 9, 1, tzinfo=timezone.utc), datetime(2026, 9, 23, tzinfo=timezone.utc))
    row = PubMedAdapter.parse_xml('<PubmedArticleSet><PubmedArticle><Article><ArticleTitle>AI <i>fatigue</i> design</ArticleTitle><ArticleDate><Year>2026</Year><Month>09</Month><Day>22</Day></ArticleDate><Abstract><AbstractText>Mixed <b>text</b>.</AbstractText></Abstract></Article></PubmedArticle></PubmedArticleSet>')[0]
    assert row.title == "AI fatigue design" and row.abstract == "Mixed text."
    assert row.published_at == "2026-09-22"
