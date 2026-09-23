from __future__ import annotations

import json
import os
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from src.models import RawRecord, SourceStatus, in_date_window
from src.venues import CNS_CORE_JOURNALS, CNS_SUBJOURNALS
from src.research_focus import FOCUS_QUERIES
from src.redaction import public_metadata, safe_error


class SerpApiScholarAdapter:
    name = "Google Scholar"

    def __init__(self, api_key: str | None = None, queries: list[str] | None = None,
                 timeout: int = 30, cache_dir: str | Path = "data/cache/scholar",
                 include_cns: bool = True):
        self.api_key = api_key if api_key is not None else os.getenv("SERPAPI_API_KEY", "")
        configured_queries = [value.strip() for value in os.getenv("SCHOLAR_QUERIES", "").split("||") if value.strip()]
        cns = " OR ".join(f'\"{journal}\"' for journal in CNS_CORE_JOURNALS + CNS_SUBJOURNALS)
        topic_queries = queries or configured_queries or list(FOCUS_QUERIES)
        self.queries = topic_queries + ([f"({cns}) {query}" for query in topic_queries] if include_cns else [])
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
        if not self.api_key:
            self._status = SourceStatus(self.name, "configuration_missing",
                                        message="SERPAPI_API_KEY is not configured")
            return []
        records: list[RawRecord] = []
        error = None
        try:
            for query in self.queries:
                params = urlencode({"engine": "google_scholar", "q": query,
                                    "api_key": self.api_key, "num": 20,
                                    "as_ylo": since.year, "as_yhi": until.year})
                cache_key = f"v2|{since.year}-{until.year}|{query}"
                cached = self._read_cache(cache_key)
                if cached is None:
                    req = Request("https://serpapi.com/search.json?" + params)
                    with urlopen(req, timeout=self.timeout) as response:
                        cached = json.loads(response.read().decode("utf-8"))
                    cached = public_metadata(cached, (self.api_key,))
                    # SerpApi reports a successful empty search in its error
                    # field. Cache that outcome too, without hiding API errors.
                    if (cached.get("search_metadata", {}).get("status") == "Success"
                            and cached.get("error") == "Google hasn't returned any results for this query."):
                        cached.pop("error")
                    if not cached.get("error"):
                        self._write_cache(cache_key, cached)
                cached = public_metadata(cached, (self.api_key,))
                if cached.get("error"):
                    raise RuntimeError(str(cached["error"]))
                records.extend(self.parse_payload(cached))
        except Exception as exc:
            error = exc
        records = list({(record.doi or record.source_id or record.title): record for record in records
                        if _in_window(record, since, until)}.values())
        if error:
            self._status = SourceStatus(self.name, _error_status(error), len(records), safe_error(error))
        else:
            self._status = SourceStatus(self.name, "ok" if records else "no_data", len(records),
                                        f"SerpApi Scholar; year filter {since.year}-{until.year}")
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
            summary = publication.get("summary", "")
            year = str(publication.get("year") or _year(summary) or "")
            link = item.get("link", "")
            doi = _doi(link) or _doi(item.get("snippet", ""))
            result.append(RawRecord(
                source="Google Scholar", source_id=str(item.get("result_id", "")),
                title=item.get("title", ""), authors=authors,
                venue=summary, abstract=item.get("snippet", ""), published_at=year,
                doi=doi, landing_url=link, oa_url=resource.get("link", ""),
                citation_count=_int((inline.get("cited_by") or {}).get("total")),
                source_score=0.75, raw_metadata=public_metadata(item),
            ))
        return result


GoogleScholarAdapter = SerpApiScholarAdapter


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _year(text: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", str(text or ""))
    return match.group(0) if match else ""


def _doi(text: str) -> str:
    match = re.search(r"(?:doi\.org/|\bdoi:\s*)(10\.\d{4,9}/[^\s<>\]\[\"']+)", str(text or ""), re.I)
    return match.group(1).rstrip(".,;)") if match else ""


def _error_status(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    message = str(exc).casefold()
    if code == 429 or "quota" in message or "rate limit" in message:
        return "quota_exhausted"
    if code in (401, 403) or "unauthorized" in message or "forbidden" in message:
        return "access_denied"
    return "error"


def _in_window(record: RawRecord, since: datetime, until: datetime) -> bool:
    # Scholar commonly exposes only a publication year. Treat that as a
    # year-level match instead of interpreting it as January 1st and dropping
    # otherwise relevant papers from a two-day run.
    if record.published_at.isdigit() and len(record.published_at) == 4:
        return since.year <= int(record.published_at) <= until.year
    return in_date_window(record.published_at, since, until)
