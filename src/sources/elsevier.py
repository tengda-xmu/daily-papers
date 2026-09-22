from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window
from src.venues import classify_venue, cns_query


class ElsevierAdapter:
    name = "Elsevier"

    def __init__(self, api_key: str | None = None, insttoken: str | None = None,
                 queries: list[str] | None = None, timeout: int = 30):
        self.api_key = api_key if api_key is not None else os.getenv("ELSEVIER_API_KEY", "")
        self.insttoken = insttoken if insttoken is not None else os.getenv("ELSEVIER_INSTTOKEN", "")
        cns = cns_query()
        topic_queries = queries or [
            "TITLE-ABS-KEY((large language model OR LLM OR agent OR generative AI) AND (predictive maintenance OR fault diagnosis OR digital twin))",
            "TITLE-ABS-KEY((generative design OR topology optimization OR surrogate model) AND (structural OR reliability))",
            "TITLE-ABS-KEY((structural fatigue OR fatigue life OR fracture) AND (AI OR machine learning OR reliability))",
        ]
        self.queries = topic_queries + [
            f"{query} AND {cns}" for query in topic_queries
            if "SRCTITLE(" not in query
        ]
        self.timeout = timeout
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if not self.api_key:
            self._status = SourceStatus(self.name, "configuration_missing",
                                        message="ELSEVIER_API_KEY is not configured")
            return []
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = f"query={quote_plus(query)}&count=25&view=COMPLETE"
                req = Request("https://api.elsevier.com/content/search/scopus?" + params,
                              headers=self._headers())
                with urlopen(req, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8-sig")
                records.extend(self.parse_body(body))
            try:
                cns_days = max(0, int(os.getenv("CNS_LOOKBACK_DAYS", "30")))
            except ValueError:
                cns_days = 30
            cns_since = since - timedelta(days=cns_days)
            records = [r for r in records if (
                in_date_window(r.published_at, since, until)
                or (classify_venue(r.venue) in ('CNS 正刊', 'CNS 子刊') and in_date_window(r.published_at, cns_since, until))
            )]
            self._status = SourceStatus(self.name, "ok", len(records))
        except Exception as exc:
            self._status = SourceStatus(self.name, _error_status(exc), len(records), str(exc))
        return records

    def _headers(self) -> dict[str, str]:
        headers = {"X-ELS-APIKey": self.api_key, "Accept": "application/json"}
        if self.insttoken:
            headers["X-ELS-Insttoken"] = self.insttoken
        return headers

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        entries = payload.get("search-results", {}).get("entry", [])
        result = []
        for item in entries:
            if not item.get("dc:title"):
                continue
            doi = item.get("prism:doi", "")
            identifier = item.get("dc:identifier", "")
            result.append(RawRecord(
                source="Elsevier", source_id=doi or identifier or item.get("eid", ""),
                title=item.get("dc:title", ""), authors=_authors(item),
                venue=item.get("prism:publicationName", ""),
                abstract=item.get("dc:description", ""),
                published_at=item.get("prism:coverDate", ""), doi=doi,
                landing_url=item.get("prism:url", ""),
                citation_count=_int(item.get("citedby-count")), source_score=0.9,
                raw_metadata=item,
            ))
        return result

    @classmethod
    def parse_body(cls, body: str) -> list[RawRecord]:
        """Parse either the JSON or XML representation returned by Scopus."""
        try:
            return cls.parse_payload(json.loads(body))
        except (json.JSONDecodeError, TypeError):
            return cls.parse_xml(body)

    @staticmethod
    def parse_xml(body: str) -> list[RawRecord]:
        root = ET.fromstring(body)
        result: list[RawRecord] = []
        for entry in root.iter():
            if _local(entry.tag) != "entry":
                continue
            fields: dict[str, str] = {}
            authors: list[str] = []
            for child in entry:
                key = _local(child.tag)
                if key == "author":
                    name = next((node.text for node in child.iter()
                                 if _local(node.tag) in ("authname", "name") and node.text), "")
                    if name:
                        authors.append(name.strip())
                elif child.text and child.text.strip():
                    fields[key] = child.text.strip()
            title = fields.get("title", "")
            if not title:
                continue
            doi = fields.get("doi", "")
            identifier = fields.get("identifier", "")
            result.append(RawRecord(
                source="Elsevier", source_id=doi or identifier or fields.get("eid", ""),
                title=title, authors=authors,
                venue=fields.get("publicationName", ""),
                abstract=fields.get("description", ""),
                published_at=fields.get("coverDate", ""), doi=doi,
                landing_url=fields.get("url", ""),
                citation_count=_int(fields.get("citedby-count")), source_score=0.9,
                raw_metadata=fields,
            ))
        return result


def _authors(item: dict) -> list[str]:
    creator = item.get("dc:creator")
    if isinstance(creator, str) and creator:
        return [creator]
    authors = item.get("author", [])
    if isinstance(authors, dict):
        authors = [authors]
    return [str(x.get("authname", x.get("name", ""))) for x in authors
            if isinstance(x, dict) and (x.get("authname") or x.get("name"))]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _error_status(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    message = str(exc).casefold()
    if code == 429 or "quota" in message or "rate limit" in message:
        return "quota_exhausted"
    if code in (401, 403) or "unauthorized" in message or "forbidden" in message:
        return "access_denied"
    return "error"
