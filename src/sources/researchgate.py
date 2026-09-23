"""ResearchGate metadata from a local export or a labelled Scholar index.

The index route calls SerpApi only. ResearchGate-hosted PDFs, cookies and
account pages are never requested or exported by this adapter.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from urllib.parse import urlsplit

from src.models import RawRecord, SourceStatus
from src.sources.google_scholar import GoogleScholarAdapter
from src.sources.researchgate_import import ResearchGateImportAdapter


INDEX_QUERY = ('site:researchgate.net ("large language model" OR "multi-agent") '
               '("fault diagnosis" OR "structural design" OR "fatigue" OR "constitutive")')


def publication_url(value: str) -> str:
    """Convert indexed profile/PDF paths to a public publication landing URL."""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in ("researchgate.net", "www.researchgate.net")
                or url.username or url.password or url.port not in (None, 443)):
            return ""
        match = re.fullmatch(r"/(?:profile/[^/]+/)?publication/(\d+(?:_[^/]*)?)(?:/.*)?", url.path)
        return "https://www.researchgate.net/publication/" + match[1] if match else ""
    except (ValueError, TypeError):
        return ""


class ResearchGateIndexAdapter(GoogleScholarAdapter):
    name = "ResearchGate"

    def __init__(self, api_key: str | None = None, **kwargs):
        # One bounded query per run; the shared year-scoped 24 h Scholar cache
        # is restored by GitHub Actions, including manual reruns.
        super().__init__(api_key=api_key, queries=[INDEX_QUERY], include_cns=False, **kwargs)

    @staticmethod
    def parse_payload(payload: dict) -> list[RawRecord]:
        records = []
        for record in GoogleScholarAdapter.parse_payload(payload):
            resources = record.raw_metadata.get("resources") or []
            candidates = [record.landing_url] + [r.get("link", "") for r in resources if isinstance(r, dict)]
            url = next((link for candidate in candidates if (link := publication_url(candidate))), "")
            if not url or not record.title:
                continue
            publication_summary = record.venue
            parts = publication_summary.split(" - ")
            # Scholar's summary is not a journal title. Retain its unmodified
            # wording as provenance, and leave the journal blank if absent.
            record.venue = re.sub(r",?\s*(?:19|20)\d{2}\s*$", "", parts[1]).strip() if len(parts) >= 3 else ""
            record.source = "ResearchGate"
            record.source_id = re.search(r"/publication/(\d+)", url)[1]
            record.landing_url = url
            record.oa_url = ""  # An indexed RG PDF is not evidence of an OA licence.
            record.source_score = 0.55
            record.raw_metadata = {
                "access_mode": "public_index", "provider": "Google Scholar via SerpApi",
                "abstract_kind": "search_snippet", "publication_summary": publication_summary,
                "date_precision": "year" if record.published_at else "unknown",
                "sources": ["ResearchGate", "Google Scholar"], "researchgate_url": url,
            }
            records.append(record)
        return records


class ResearchGateAdapter:
    name = "ResearchGate"

    def __init__(self, importer=None, index=None, public_index: bool | None = None):
        self.importer = importer if importer is not None else ResearchGateImportAdapter()
        self.index = index if index is not None else ResearchGateIndexAdapter()
        self.public_index = (os.getenv("RESEARCHGATE_PUBLIC_INDEX", "1").casefold() not in ("0", "false", "off")
                             if public_index is None else public_index)
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        local = self.importer.fetch(since, until)
        local_status = self.importer.status
        if not self.public_index or (local and local_status.status == "ok"):
            self._status = local_status
            return local
        indexed = self.index.fetch(since, until)
        index_status = self.index.status
        if index_status.status in ("ok", "no_data"):
            rows = {}
            for record in indexed + local:
                url = publication_url(record.landing_url)
                key = re.search(r"/publication/(\d+)", url)[1] if url else record.doi or record.source_id
                # Prefer local metadata when both modes contain the same URL.
                rows[key] = record
            records = list(rows.values())
            message = (f"公开索引已接入：Google Scholar / SerpApi 本轮返回 {len(indexed)} 条 ResearchGate 元数据。"
                       " 按发表年份检索；摘要为检索片段，未注明年份的记录保留为待核验线索。")
            if local:
                message += f" 另保留本地导出；{local_status.message}"
            else:
                message += " 当前使用公开索引，本地浏览器采集尚未产出本期记录。"
            self._status = SourceStatus(self.name, "ok" if records else "no_data", len(records), message)
            return records
        if local:
            self._status = SourceStatus(self.name, "partial", len(local),
                                       f"本地连接器已导入；{local_status.message} 公开索引本轮不可用（{index_status.status}）。")
            return local
        if index_status.status == "configuration_missing" and local_status.status == "no_data":
            self._status = local_status
            return []
        message = ("ResearchGate 本地导出不可用；公开索引需要 SERPAPI_API_KEY。"
                   if index_status.status == "configuration_missing" else
                   f"ResearchGate 本地导出不可用；公开索引本轮失败（{index_status.status}）：{index_status.message}")
        self._status = SourceStatus(self.name, index_status.status, 0, message)
        return []
