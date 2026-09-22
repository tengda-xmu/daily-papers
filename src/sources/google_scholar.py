from __future__ import annotations

import json
import os
import hashlib
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.models import RawRecord, SourceStatus


class SerpApiScholarAdapter:
    name = "Google Scholar"

    def __init__(self, api_key: str | None = None, queries: list[str] | None = None,
                 timeout: int = 30, cache_dir: str | Path = "data/cache/scholar"):
        self.api_key = api_key if api_key is not None else os.getenv("SERPAPI_API_KEY", "")
        configured_queries = [value.strip() for value in os.getenv("SCHOLAR_QUERIES", "").split("||") if value.strip()]
        self.queries = queries or configured_queries or [
            "AI predictive maintenance",
            "generative structural design reliability",
            "AI structural fatigue reliability",
        ]
        self.timeout = timeout
        self.cache_dir = Path(cache_dir)
        try:
            self.cache_ttl = max(0, int(os.getenv("SCHOLAR_CACHE_TTL", "86400")))
        except ValueError:
            self.cache_ttl = 86400
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        del since, until
        if not self.api_key:
            self._status = SourceStatus(self.name, "configuration_missing",
                                        message="SERPAPI_API_KEY is not configured")
            return []
        records: list[RawRecord] = []
        try:
            for query in self.queries:
                params = urlencode({"engine": "google_scholar", "q": query,
                                    "api_key": self.api_key, "num": 20})
                cached = self._read_cache(query)
                if cached is None:
                    req = Request("https://serpapi.com/search.json?" + params)
                    with urlopen(req, timeout=self.timeout) as response:
                        cached = json.loads(response.read().decode("utf-8"))
                    self._write_cache(query, cached)
                records.extend(self.parse_payload(cached))
            self._status = SourceStatus(self.name, "ok", len(records))
        except Exception as exc:
            self._status = SourceStatus(self.name, "error", len(records), str(exc))
        return records

    def _cache_path(self, query: str) -> Path:
        key = hashlib.sha256(query.encode("utf-8")).hexdigest()[:20]
        return self.cache_dir / f"{key}.json"

    def _read_cache(self, query: str) -> dict | None:
        path = self._cache_path(query)
        if self.cache_ttl == 0 or not path.exists():
            return None
        try:
            if time.time() - path.stat().st_mtime > self.cache_ttl:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, query: str, payload: dict) -> None:
        if self.cache_ttl == 0:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(query).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        result = []
        for item in payload.get("organic_results", []):
            publication = item.get("publication_info") or {}
            authors = [a.get("name", "") for a in publication.get("authors", [])
                       if isinstance(a, dict) and a.get("name")]
            inline = item.get("inline_links") or {}
            resources = item.get("resources") or []
            resource = resources[0] if resources and isinstance(resources[0], dict) else {}
            result.append(RawRecord(
                source="Google Scholar", source_id=str(item.get("result_id", "")),
                title=item.get("title", ""), authors=authors,
                venue=publication.get("summary", ""), abstract=item.get("snippet", ""),
                landing_url=item.get("link", ""), oa_url=resource.get("link", ""),
                citation_count=_int((inline.get("cited_by") or {}).get("total")),
                source_score=0.75, raw_metadata=item,
            ))
        return result


GoogleScholarAdapter = SerpApiScholarAdapter


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
