from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window
from src.venues import classify_venue, cns_query
from src.redaction import public_metadata, safe_error


class ElsevierAdapter:
    name = "Elsevier"

    def __init__(self, api_key: str | None = None, insttoken: str | None = None,
                 queries: list[str] | None = None, timeout: int = 30,
                 manual: bool = False, limit: int = 25):
        self.api_key = api_key if api_key is not None else os.getenv("ELSEVIER_API_KEY", "")
        self.insttoken = insttoken if insttoken is not None else os.getenv("ELSEVIER_INSTTOKEN", "")
        cns = cns_query()
        topic_queries = queries or [
            'TITLE-ABS-KEY(("large language model" OR LLM OR "multi-agent" OR "generative AI") AND ("predictive maintenance" OR "fault diagnosis" OR "digital twin"))',
            'TITLE-ABS-KEY(("generative design" OR "topology optimization" OR "surrogate model") AND (structural OR reliability))',
            'TITLE-ABS-KEY(("structural fatigue" OR "fatigue life" OR fracture) AND (AI OR "machine learning" OR reliability))',
        ]
        self.queries = topic_queries + ([] if manual else [
            f"{query} AND {cns}" for query in topic_queries
            if "SRCTITLE(" not in query
        ])
        self.timeout = timeout
        self.manual = manual
        self.limit = max(1, min(25, limit))
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if not self.api_key:
            self._status = SourceStatus(self.name, "configuration_missing",
                                        message="ELSEVIER_API_KEY is not configured")
            return []
        if self.manual:
            return self._fetch_manual(since, until)
        records: list[RawRecord] = []
        view, restricted = "COMPLETE", False
        error = None
        try:
            cns_days = max(0, int(os.getenv("CNS_LOOKBACK_DAYS", "180") or "180"))
        except ValueError:
            cns_days = 180
        cns_since = until - timedelta(days=cns_days)
        first_year = min(since, cns_since).year
        years = str(until.year) if first_year == until.year else f"{first_year}-{until.year}"
        try:
            for query in self.queries:
                def search(selected_view):
                    params = urlencode({"query": query, "count": 25, "view": selected_view,
                                        "date": years})
                    req = Request("https://api.elsevier.com/content/search/scopus?" + params,
                                  headers=self._headers())
                    with urlopen(req, timeout=self.timeout) as response:
                        return response.read().decode("utf-8-sig")
                try:
                    body = search(view)
                except HTTPError as exc:
                    if exc.code not in (401, 403) or view != "COMPLETE":
                        raise
                    # STANDARD is a separate, supported metadata view. Reuse
                    # it for the rest of this run when COMPLETE is unavailable.
                    view, restricted = "STANDARD", True
                    body = search(view)
                body = public_metadata(body, (self.api_key, self.insttoken))
                records.extend(self.parse_body(body))
        except Exception as exc:
            error = exc
        records = list({(r.doi or r.source_id or r.title): r for r in records if (
            in_date_window(r.published_at, since, until)
            or (classify_venue(r.venue) in ('CNS 正刊', 'CNS 子刊') and in_date_window(r.published_at, cns_since, until))
        )}.values())
        message = f"Scopus {view} metadata"
        if restricted:
            message += "; COMPLETE unavailable under current authorization"
        if error:
            self._status = SourceStatus(self.name, _error_status(error), len(records), message + "; " + safe_error(error))
        else:
            self._status = SourceStatus(self.name, "ok" if records else "no_data", len(records), message)
        return records

    def _fetch_manual(self, since: datetime, until: datetime) -> list[RawRecord]:
        """Rank by relevance and fill the requested count after exact-date filtering.

        Scopus's date parameter only supports years; coverDate can be a future
        issue date. A date-sorted first page must not become a false no_data.
        Bound this interactive operation to three pages and one transport retry.
        """
        years = str(until.year) if since.year == until.year else f"{since.year}-{until.year}"
        records, scanned, pages, start = {}, 0, 0, 0
        more, total, error, retries = False, None, None, 1
        deadline = time.monotonic() + 42
        try:
            for _ in range(3):
                params = urlencode({'query': self.queries[0], 'count': 25, 'start': start,
                                    'view': 'STANDARD', 'date': years, 'sort': 'relevancy'})
                request = Request('https://api.elsevier.com/content/search/scopus?' + params,
                                  headers=self._headers())
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    try:
                        with urlopen(request, timeout=min(self.timeout, remaining)) as response:
                            payload = json.loads(response.read().decode('utf-8-sig'))
                        break
                    except HTTPError:
                        # Authorization and quota errors must not be retried.
                        raise
                    except (URLError, TimeoutError, ConnectionError):
                        if not retries:
                            raise
                        retries -= 1
                payload = public_metadata(payload, (self.api_key, self.insttoken))
                service_error = payload.get('service-error', {}).get('status', {})
                if service_error:
                    code = {'AUTHORIZATION_ERROR': 403, 'AUTHENTICATION_ERROR': 401,
                            'QUOTA_EXCEEDED': 429, 'INVALID_INPUT': 400}.get(service_error.get('statusCode'), 502)
                    raise HTTPError('https://api.elsevier.com/content/search/scopus', code, 'Scopus API error', {}, None)
                results = payload.get('search-results')
                if not isinstance(results, dict):
                    raise ValueError('Missing Scopus search results')
                entries = results.get('entry', [])
                if not isinstance(entries, list):
                    raise ValueError('Invalid Scopus entries')
                total = _int(results.get('opensearch:totalResults'))
                pages += 1
                # RESULT_NOT_FOUND is Scopus's documented empty-search entry.
                if any(item.get('error') and item.get('error') != 'RESULT_NOT_FOUND' for item in entries):
                    raise ValueError('Scopus returned an error entry')
                items = [item for item in entries if item.get('dc:title')]
                scanned += len(items)
                for row in self.parse_payload(payload):
                    if in_date_window(row.published_at, since, until):
                        records.setdefault(row.doi or row.source_id or row.title, row)
                start += len(entries)
                more = start < total if total is not None else len(entries) == 25
                if not items and more:
                    raise ValueError('Scopus returned no usable entries for a nonempty result set')
                if len(records) >= self.limit or not more:
                    break
        except Exception as exc:
            error = exc
        rows = list(records.values())[:self.limit]
        message = f'Scopus STANDARD：按相关性读取 {pages} 页、{scanned} 条记录，所选日期内返回 {len(rows)} 条。'
        if total is not None:
            message += f' 年份范围共匹配 {total} 条，未逐条扫描全部结果。'
        message += ' 日期按 Scopus 期刊日期筛选；标准视图的摘要、作者字段可能不完整。'
        if error:
            state = 'partial' if rows else _error_status(error)
            detail = safe_error(error)
            if state == 'access_denied' or getattr(error, 'code', None) in (401, 403):
                message += ' 标准视图访问被拒绝，请检查 API Key 和机构授权。'
            elif state == 'quota_exhausted' or getattr(error, 'code', None) == 429:
                message += ' Scopus 请求限频或额度已用完，已有结果保留。'
            else:
                message += ' 请求未完成，已有结果保留；可重试。'
            message += f'（{detail}）'
        elif more and len(rows) < self.limit:
            state = 'partial'
            message += ' 已达本次 3 页检索上限；日期筛选后不足所选数量，不代表全库没有更多匹配。'
        else:
            state = 'ok' if rows else 'no_data'
        self._status = SourceStatus(self.name, state, len(rows), message)
        return rows

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
                landing_url=f"https://doi.org/{doi}" if doi else item.get("prism:url", ""),
                citation_count=_int(item.get("citedby-count")), source_score=0.9,
                raw_metadata=public_metadata(item),
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
                landing_url=f"https://doi.org/{doi}" if doi else fields.get("url", ""),
                citation_count=_int(fields.get("citedby-count")), source_score=0.9,
                raw_metadata=public_metadata(fields),
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
