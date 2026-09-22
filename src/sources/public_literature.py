"""Keyless public literature adapters used to keep the daily pipeline useful.

These APIs expose bibliographic metadata without a user login. They are used
for discovery and deduplication; publisher-specific credentials remain
available for Elsevier, SerpApi Scholar and Web of Science when configured.
"""
from __future__ import annotations

import json
import os
import re
import html
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window


TOPIC_QUERIES = (
    "machine learning predictive maintenance",
    "generative structural design reliability",
    "machine learning structural fatigue",
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
        records = list({(r.doi or r.source_id or r.title): r for r in records
                        if r.title and in_date_window(r.published_at, since, until)}.values())
        self._status = SourceStatus(self.name, "ok" if records else "no_data", len(records),
                                    "public API" if records else "public API returned no matching records")
        return records

    def _fail(self, records: list[RawRecord], exc: Exception) -> list[RawRecord]:
        code = getattr(exc, "code", None)
        state = "quota_exhausted" if code == 429 else "access_denied" if code in (401, 403) else "error"
        self._status = SourceStatus(self.name, state, len(records),
                                    f"HTTP {code}" if code else type(exc).__name__)
        return records


class ArxivAdapter(PublicLiteratureAdapter):
    name = "arXiv"

    def __init__(self, queries=None, **kwargs):
        super().__init__(queries=queries or ["predictive maintenance", "fault diagnosis",
                         "generative design", "topology optimization", "structural fatigue"], **kwargs)

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            query = " OR ".join(f'all:"{term}"' for term in self.queries)
            params = urlencode({"search_query": query, "start": 0, "max_results": 50,
                                "sortBy": "submittedDate", "sortOrder": "descending"}, quote_via=quote)
            contact = os.getenv("ARXIV_CONTACT", "").strip() or "https://github.com/tengda-xmu/daily-papers"
            query_url = "https://export.arxiv.org/api/query?" + params
            headers = {"Accept": "application/atom+xml", "User-Agent": f"daily-papers/1.0 ({contact})"}
            try:
                body = self._get_text(query_url, headers)
            except HTTPError as exc:
                if exc.code not in (406, 429, 500, 502, 503, 504):
                    raise
                # Daily RSS is a separate, documented metadata service; keep
                # the acquisition method visible instead of disguising it.
                body = self._get_text("https://rss.arxiv.org/rss/cs.AI+cs.LG+cs.CE+physics.comp-ph",
                                      {"Accept": "application/rss+xml"})
                result = self._finish(self.parse_rss(body), since, until)
                self._status.message = f"Official arXiv RSS fallback (search API HTTP {exc.code})"
                return result
            return self._finish(self.parse_xml(body), since, until)
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
            if fields.get("id", "").startswith("http://arxiv.org/api/errors"):
                raise ValueError("arXiv returned an API error entry")
            authors = [(child.findtext("{*}name") or "").strip() for child in entry
                       if _local(child.tag) == "author" and child.findtext("{*}name")]
            result.append(RawRecord(source=adapter.name, source_id=fields.get("id", ""),
                                    title=fields.get("title", ""), authors=authors,
                                    abstract=fields.get("summary", ""), published_at=fields.get("published", ""),
                                    landing_url=fields.get("id", ""), oa_url=fields.get("id", ""),
                                    doi=fields.get("doi", ""), venue=fields.get("journal_ref", ""),
                                    source_score=.62, raw_metadata={"provider": "arXiv", "method": "api"}))
        return result

    @staticmethod
    def parse_rss(body: str) -> list[RawRecord]:
        result = []
        for item in ET.fromstring(body).findall(".//item"):
            fields = {_local(child.tag): "".join(child.itertext()).strip() for child in item}
            abstract = fields.get("description", "").split("Abstract:", 1)[-1]
            date = fields.get("pubDate", "")
            if date:
                date = parsedate_to_datetime(date).isoformat()
            result.append(RawRecord(
                "arXiv", fields.get("guid", fields.get("link", "")), fields.get("title", ""),
                authors=fields.get("creator", ""), abstract=html.unescape(re.sub(r"<[^>]+>", "", abstract)),
                published_at=date, landing_url=fields.get("link", ""), oa_url=fields.get("link", ""),
                source_score=.62, raw_metadata={"provider": "arXiv", "method": "rss", "date_kind": "announcement"},
            ))
        return result


class OpenAlexAdapter(PublicLiteratureAdapter):
    name = "OpenAlex"

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = {"search": query, "filter": f"from_publication_date:{since.date()},to_publication_date:{until.date()}",
                          "per-page": 25, "mailto": os.getenv("OPENALEX_MAILTO", "")}
                headers = {"Authorization": "Bearer " + os.environ["OPENALEX_API_KEY"]} if os.getenv("OPENALEX_API_KEY") else {}
                payload = self._get_json("https://api.openalex.org/works?" + urlencode({k: v for k, v in params.items() if v}), headers)
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
            for index, query in enumerate(self.queries):
                if index:
                    time.sleep(2)
                params = {"query": query, "filter": f"from-pub-date:{since.date()},until-pub-date:{until.date()}",
                          "rows": 25, "select": "DOI,title,author,container-title,abstract,published,URL,is-referenced-by-count"}
                url = "https://api.crossref.org/works?" + urlencode(params)
                headers = {"Accept": "application/json",
                           "User-Agent": "daily-papers/1.0 (https://github.com/tengda-xmu/daily-papers)"}
                try:
                    payload = self._get_json(url, headers)
                except HTTPError as exc:
                    delay = exc.headers.get("Retry-After", "10") if exc.headers else "10"
                    if exc.code != 429 or not delay.isdigit() or int(delay) > 30:
                        raise
                    time.sleep(max(10, int(delay)))
                    payload = self._get_json(url, headers)
                records.extend(self.parse_payload(payload))
            return self._finish(records, since, until)
        except Exception as exc:
            return self._fail(self._finish(records, since, until), exc)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        result = []
        for item in (payload.get("message") or {}).get("items", []):
            title = (item.get("title") or [""])[0]
            date_parts = (item.get("published") or {}).get("date-parts") or [[]]
            date = "-".join(str(x) if i == 0 else str(x).zfill(2) for i, x in enumerate(date_parts[0]))
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

    def __init__(self, queries=None, **kwargs):
        super().__init__(queries=queries or [
            '("predictive maintenance" | "fault diagnosis" | "structural fatigue" | "topology optimization" | "structural reliability") + ("machine learning" | "deep learning" | "large language model" | "generative design")'
        ], **kwargs)

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = {"query": query, "sort": "publicationDate:desc",
                          "publicationDateOrYear": f"{since.date()}:{until.date()}",
                          "fields": "title,authors,abstract,year,publicationDate,venue,externalIds,url,openAccessPdf,citationCount"}
                headers = {}
                if os.getenv("SEMANTIC_SCHOLAR_API_KEY", "").strip():
                    headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"].strip()
                url = "https://api.semanticscholar.org/graph/v1/paper/search/bulk?" + urlencode(params)
                try:
                    payload = self._get_json(url, headers)
                except HTTPError as exc:
                    delay = exc.headers.get("Retry-After", "5") if exc.headers else "5"
                    if exc.code != 429 or not delay.isdigit() or int(delay) > 30:
                        raise
                    time.sleep(max(5, int(delay)))
                    payload = self._get_json(url, headers)
                records.extend(self.parse_payload(payload))
                time.sleep(1)
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
                abstract=item.get("abstract", ""), published_at=item.get("publicationDate") or str(item.get("year") or ""), doi=doi,
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
            title_node = article.find(".//ArticleTitle")
            title = "".join(title_node.itertext()) if title_node is not None else ""
            pmid = article.findtext(".//PMID", default="")
            abstract = " ".join("".join(x.itertext()) for x in article.findall(".//AbstractText"))
            journal = article.findtext(".//Journal/Title", default="")
            year = article.findtext(".//PubDate/Year", default="") or article.findtext(".//PubDate/MedlineDate", default="")[:4]
            date_node = article.find(".//ArticleDate")
            if date_node is None:
                date_node = article.find(".//PubDate")
            published = year
            if date_node is not None:
                date_year, month, day = (date_node.findtext(k, "") for k in ("Year", "Month", "Day"))
                if date_year and month and day:
                    try:
                        month_num = int(month) if month.isdigit() else datetime.strptime(month[:3], "%b").month
                        published = datetime(int(date_year), month_num, int(day)).date().isoformat()
                    except ValueError:
                        pass
            authors = []
            for author in article.findall(".//Author"):
                name = " ".join(x for x in (author.findtext("LastName", ""), author.findtext("ForeName", "")) if x)
                if name:
                    authors.append(name)
            doi = next((x.text or "" for x in article.findall(".//ArticleId") if x.attrib.get("IdType") == "doi"), "")
            result.append(RawRecord(source="PubMed", source_id=pmid, title=title, authors=authors, venue=journal,
                                    abstract=abstract, published_at=published, doi=doi,
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
                    "https://api.clarivate.com/apis/wos-starter/v1/documents?" + urlencode({
                        "q": f"TS=({query}) AND PY=({since.year}-{until.year})", "limit": 25}),
                    {"X-ApiKey": self.api_key, "Accept": "application/json"},
                )
                records.extend(self.parse_payload(payload))
                time.sleep(1)
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
                source = metadata.get("source") or {}
                authors = (metadata.get("names") or {}).get("authors") or []
                citations = next((c.get("count") for c in metadata.get("citations", []) if c.get("db") == "WOS"), None)
                records.append(RawRecord(source="Web of Science", source_id=str(metadata.get("uid", "")), title=title,
                                         authors=[a.get("displayName") or a.get("wosStandard", "") for a in authors],
                                         venue=source.get("sourceTitle", ""), published_at=str(source.get("publishYear") or ""),
                                         doi=(metadata.get("identifiers") or {}).get("doi", ""), citation_count=citations,
                                         landing_url=metadata.get("links", {}).get("record", "") if isinstance(metadata.get("links"), dict) else "",
                                         source_score=.85, raw_metadata=item))
        return records


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
