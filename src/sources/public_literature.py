"""Keyless public literature adapters used to keep the daily pipeline useful.

These APIs expose bibliographic metadata without a user login. They are used
for discovery and deduplication; publisher-specific credentials remain
available for Elsevier, SerpApi Scholar and Web of Science when configured.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window


TOPIC_QUERIES = (
    "large language model predictive maintenance fault diagnosis",
    "generative design structural optimization reliability",
    "structural fatigue machine learning reliability",
)


class PublicLiteratureAdapter:
    name = ""
    timeout = 25

    def __init__(self, queries: list[str] | None = None, timeout: int = 25):
        self.queries = queries or list(TOPIC_QUERIES)
        self.timeout = timeout
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        request = Request(url, headers={"User-Agent": "daily-papers/1.0 (research digest)", **(headers or {})})
        with urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8-sig"))

    def _get_text(self, url: str, headers: dict[str, str] | None = None) -> str:
        request = Request(url, headers={"User-Agent": "daily-papers/1.0 (research digest)", **(headers or {})})
        with urlopen(request, timeout=self.timeout) as response:
            return response.read().decode("utf-8-sig")

    def _finish(self, records: list[RawRecord], since: datetime, until: datetime) -> list[RawRecord]:
        records = [record for record in records if in_date_window(record.published_at, since, until)]
        self._status = SourceStatus(self.name, "ok" if records else "no_data", len(records),
                                    "public API" if records else "public API returned no matching records")
        return records

    def _fail(self, records: list[RawRecord], exc: Exception) -> list[RawRecord]:
        code = getattr(exc, "code", None)
        state = "quota_exhausted" if code == 429 or "rate" in str(exc).casefold() else "error"
        self._status = SourceStatus(self.name, state, len(records), str(exc))
        return records


class ArxivAdapter(PublicLiteratureAdapter):
    name = "arXiv"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            query = " OR ".join(f'all:"{term}"' for term in self.queries)
            params = urlencode({"search_query": query, "start": 0, "max_results": 50,
                                "sortBy": "submittedDate", "sortOrder": "descending"})
            contact = os.getenv("ARXIV_CONTACT", "").strip() or "https://github.com/tengda-xmu/daily-papers"
            root = ET.fromstring(self._get_text(
                "https://export.arxiv.org/api/query?" + params,
                {"Accept": "application/atom+xml", "User-Agent": f"daily-papers/1.0 ({contact})"},
            ))
            for entry in root:
                if _local(entry.tag) != "entry":
                    continue
                fields = {_local(child.tag): (child.text or "").strip() for child in entry}
                authors = [(child.findtext("{*}name") or "").strip() for child in entry
                           if _local(child.tag) == "author" and child.findtext("{*}name")]
                records.append(RawRecord(
                    source=self.name, source_id=fields.get("id", ""), title=fields.get("title", ""),
                    authors=authors, abstract=fields.get("summary", ""),
                    published_at=fields.get("published", ""), landing_url=fields.get("id", ""),
                    oa_url=fields.get("id", ""), source_score=.62,
                    raw_metadata={"provider": "arXiv", "id": fields.get("id", "")},
                ))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_xml(body: str) -> list[RawRecord]:
        adapter = ArxivAdapter()
        root = ET.fromstring(body)
        result: list[RawRecord] = []
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            fields = {_local(child.tag): (child.text or "").strip() for child in entry}
            authors = [(child.findtext("{*}name") or "").strip() for child in entry
                       if _local(child.tag) == "author" and child.findtext("{*}name")]
            result.append(RawRecord(source=adapter.name, source_id=fields.get("id", ""),
                                    title=fields.get("title", ""), authors=authors,
                                    abstract=fields.get("summary", ""), published_at=fields.get("published", ""),
                                    landing_url=fields.get("id", ""), oa_url=fields.get("id", ""),
                                    source_score=.62))
        return result


class OpenAlexAdapter(PublicLiteratureAdapter):
    name = "OpenAlex"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = {"search": query, "filter": f"from_publication_date:{since.date()},to_publication_date:{until.date()}",
                          "per-page": 25, "mailto": os.getenv("OPENALEX_MAILTO", "")}
                payload = self._get_json("https://api.openalex.org/works?" + urlencode({k: v for k, v in params.items() if v}))
                records.extend(self.parse_payload(payload))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        result = []
        for item in payload.get("results", []):
            inverted = item.get("abstract_inverted_index") or {}
            positions = [(position, word) for word, indexes in inverted.items()
                         for position in (indexes or [])]
            abstract = " ".join(word for _, word in sorted(positions))
            location = item.get("primary_location") or {}
            source = location.get("source") or {}
            authors = [(a.get("author") or {}).get("display_name", "") for a in item.get("authorships", [])]
            doi = str(item.get("doi") or "").replace("https://doi.org/", "")
            result.append(RawRecord(
                source="OpenAlex", source_id=str(item.get("id", "")), title=item.get("title", ""),
                authors=[a for a in authors if a], venue=source.get("display_name", ""), abstract=abstract,
                published_at=item.get("publication_date", ""), doi=doi,
                landing_url=item.get("doi") or item.get("id", ""),
                oa_url=(item.get("open_access") or {}).get("oa_url", ""),
                citation_count=item.get("cited_by_count"), source_score=.68,
                raw_metadata={"provider": "OpenAlex", "id": item.get("id", "")},
            ))
        return result


class CrossrefAdapter(PublicLiteratureAdapter):
    name = "Crossref"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = {"query": query, "filter": f"from-pub-date:{since.date()},until-pub-date:{until.date()}",
                          "rows": 25, "select": "DOI,title,author,container-title,abstract,published,URL,is-referenced-by-count"}
                payload = self._get_json("https://api.crossref.org/works?" + urlencode(params),
                                         {"Accept": "application/json"})
                records.extend(self.parse_payload(payload))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        result = []
        for item in (payload.get("message") or {}).get("items", []):
            title = (item.get("title") or [""])[0]
            date_parts = (item.get("published") or {}).get("date-parts") or [[]]
            date = "-".join(str(x) for x in date_parts[0]) if date_parts[0] else ""
            authors = [" ".join(x for x in (a.get("given", ""), a.get("family", "")) if x)
                       for a in item.get("author", [])]
            abstract = re.sub(r"<[^>]+>", "", item.get("abstract", ""))
            result.append(RawRecord(
                source="Crossref", source_id=item.get("DOI", ""), title=title, authors=authors,
                venue=(item.get("container-title") or [""])[0], abstract=abstract, published_at=date,
                doi=item.get("DOI", ""), landing_url=item.get("URL", ""),
                citation_count=item.get("is-referenced-by-count"), source_score=.58,
                raw_metadata={"provider": "Crossref", "publisher": item.get("publisher", "")},
            ))
        return result


class SemanticScholarAdapter(PublicLiteratureAdapter):
    name = "Semantic Scholar"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = {"query": query, "limit": 20,
                          "fields": "title,authors,abstract,year,venue,externalIds,url,openAccessPdf,citationCount"}
                headers = {}
                if os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip():
                    headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"].strip()
                payload = self._get_json("https://api.semanticscholar.org/graph/v1/paper/search?" + urlencode(params), headers)
                records.extend(self.parse_payload(payload))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        result = []
        for item in payload.get("data", []):
            external = item.get("externalIds") or {}
            doi = external.get("DOI", "")
            pdf = item.get("openAccessPdf") or {}
            result.append(RawRecord(
                source="Semantic Scholar", source_id=str(item.get("paperId", "")), title=item.get("title", ""),
                authors=[a.get("name", "") for a in item.get("authors", [])], venue=item.get("venue", ""),
                abstract=item.get("abstract", ""), published_at=str(item.get("year") or ""), doi=doi,
                landing_url=item.get("url", ""), oa_url=pdf.get("url", ""),
                citation_count=item.get("citationCount"), source_score=.64,
                raw_metadata={"provider": "Semantic Scholar", "paperId": item.get("paperId", "")},
            ))
        return result


class PubMedAdapter(PublicLiteratureAdapter):
    name = "PubMed"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            term = " OR ".join(f'({q})' for q in self.queries)
            params = {"db": "pubmed", "term": term, "retmode": "json", "retmax": 50,
                      "mindate": since.strftime("%Y/%m/%d"), "maxdate": until.strftime("%Y/%m/%d"),
                      "datetype": "pdat"}
            found = self._get_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urlencode(params))
            ids = (found.get("esearchresult") or {}).get("idlist", [])
            if ids:
                xml = self._get_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?" + urlencode({"db": "pubmed", "id": ",".join(ids), "retmode": "xml"}))
                records = self.parse_xml(xml)
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_xml(body: str) -> list[RawRecord]:
        result = []
        root = ET.fromstring(body)
        for article in root.findall(".//PubmedArticle"):
            title = " ".join(article.findtext(".//ArticleTitle", default="").split())
            pmid = article.findtext(".//PMID", default="")
            abstract = " ".join(x.text or "" for x in article.findall(".//AbstractText"))
            journal = article.findtext(".//Journal/Title", default="")
            year = article.findtext(".//PubDate/Year", default="") or article.findtext(".//PubDate/MedlineDate", default="")[:4]
            authors = []
            for author in article.findall(".//Author"):
                name = " ".join(x for x in (author.findtext("LastName", ""), author.findtext("ForeName", "")) if x)
                if name:
                    authors.append(name)
            doi = next((x.text or "" for x in article.findall(".//ArticleId") if x.attrib.get("IdType") == "doi"), "")
            result.append(RawRecord(source="PubMed", source_id=pmid, title=title, authors=authors, venue=journal,
                                    abstract=abstract, published_at=year, doi=doi,
                                    landing_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                                    source_score=.55, raw_metadata={"provider": "PubMed", "pmid": pmid}))
        return result


class WebOfScienceAdapter(PublicLiteratureAdapter):
    """Clarivate Starter API adapter; key is intentionally never bundled."""
    name = "Web of Science"

    def __init__(self, api_key: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.api_key = api_key if api_key is not None else os.getenv("WOS_API_KEY", "").strip()

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if not self.api_key:
            self._status = SourceStatus(self.name, "authorization_required", 0,
                                        "Clarivate Web of Science API key is required")
            return []
        # The endpoint is deliberately isolated so an institutional key can be
        # enabled without changing the public adapters.
        try:
            records: list[RawRecord] = []
            for query in self.queries:
                payload = self._get_json(
                    "https://api.clarivate.com/apis/wos-starter/v1/documents?" + urlencode({"q": query, "limit": 25}),
                    {"X-ApiKey": self.api_key, "Accept": "application/json"},
                )
                records.extend(self.parse_payload(payload))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(records, exc)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        records = []
        for item in payload.get("hits", payload.get("records", [])):
            metadata = item.get("metadata", item)
            title = metadata.get("title", "") if isinstance(metadata, dict) else ""
            if isinstance(title, dict):
                title = title.get("value", "")
            if title:
                records.append(RawRecord(source="Web of Science", source_id=str(metadata.get("uid", "")), title=title,
                                         landing_url=metadata.get("links", {}).get("record", "") if isinstance(metadata.get("links"), dict) else "",
                                         source_score=.85, raw_metadata=item))
        return records


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
