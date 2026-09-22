from __future__ import annotations

import json
import os
from datetime import datetime
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from src.models import RawRecord, SourceStatus, in_date_window


class ElsevierAdapter:
    name = "Elsevier"

    def __init__(self, api_key: str | None = None, insttoken: str | None = None,
                 queries: list[str] | None = None, timeout: int = 30):
        self.api_key = api_key if api_key is not None else os.getenv("ELSEVIER_API_KEY", "")
        self.insttoken = insttoken if insttoken is not None else os.getenv("ELSEVIER_INSTTOKEN", "")
        self.queries = queries or ["TITLE-ABS-KEY(artificial intelligence AND predictive maintenance)"]
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
                    records.extend(self.parse_payload(json.loads(response.read().decode("utf-8"))))
            records = [r for r in records if in_date_window(r.published_at, since, until)]
            self._status = SourceStatus(self.name, "ok", len(records))
        except Exception as exc:
            self._status = SourceStatus(self.name, "error", len(records), str(exc))
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
