from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import RawRecord, SourceStatus, in_date_window, parse_date


class ResearchGateImportAdapter:
    name = "ResearchGate"

    def __init__(self, paths: list[str] | None = None):
        configured = os.getenv("RESEARCHGATE_IMPORT_PATH", "data/inbox/researchgate.json")
        self.paths = [Path(p) for p in (paths or [configured])]
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        records: list[RawRecord] = []
        timestamps, failed_pages = [], 0
        try:
            for path in self.paths:
                if not path.exists():
                    continue
                text = path.read_text(encoding="utf-8-sig")
                suffix = path.suffix.lower()
                if suffix == ".csv":
                    records.extend(self.parse_csv(text))
                elif suffix in (".bib", ".bibtex"):
                    records.extend(self.parse_bibtex(text))
                else:
                    payload = json.loads(text)
                    records.extend(self.parse_json(payload))
                    if isinstance(payload, dict):
                        stamp = parse_date(payload.get("exported_at"))
                        if stamp:
                            timestamps.append(stamp)
                        failures = payload.get("failed_pages", 0)
                        if type(failures) is int and failures > 0:
                            failed_pages += failures
            imported = len(records)
            records = [r for r in records if in_date_window(r.published_at, since, until)]
            if imported:
                message = f"本地连接器已导入 {imported} 条元数据，本期符合日期范围 {len(records)} 条。"
                status = "ok" if records else "no_data"
                if timestamps:
                    latest = max(timestamps)
                    china = timezone(timedelta(hours=8))
                    message += f" 最近同步：{latest.astimezone(china):%m-%d %H:%M}（北京时间）。"
                    now = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
                    if now - latest > timedelta(days=3):
                        status = "partial"
                        message += " 导出已超过 3 天，请检查本机定时任务或重新登录。"
                if failed_pages:
                    status = "partial"
                    message += f" {failed_pages} 个页面访问失败，已保留其余结果。"
                self._status = SourceStatus(self.name, status, len(records), message)
            elif not any(path.exists() for path in self.paths):
                self._status = SourceStatus(
                    self.name, "configuration_missing", 0,
                    "ResearchGate export is not available; run the local connector",
                )
            else:
                self._status = SourceStatus(self.name, "no_data", 0, "export is empty")
        except Exception as exc:
            self._status = SourceStatus(self.name, "error", len(records), str(exc))
        return records

    @staticmethod
    def parse_json(payload) -> list[RawRecord]:
        if isinstance(payload, dict):
            items = payload.get("records", payload.get("items", payload.get("data", [])))
        else:
            items = payload
        return [_record(i) for i in (items or [])
                if isinstance(i, dict) and i.get("title")]

    @staticmethod
    def parse_csv(text: str) -> list[RawRecord]:
        return [_record(row) for row in csv.DictReader(text.splitlines())
                if row.get("title")]

    @staticmethod
    def parse_bibtex(text: str) -> list[RawRecord]:
        result = []
        for block in re.findall(r"@\w+\s*\{.*?(?=\n@|\Z)", text.strip(), flags=re.S | re.I):
            fields = {}
            for key, value in re.findall(
                    r"(?im)\b([\w-]+)\s*=\s*[{\"]([\s\S]*?)[}\"]\s*,?", block):
                fields[key.lower()] = re.sub(r"\s+", " ", value).strip()
            record = _record(fields)
            if record.title:
                result.append(record)
        return result


def _record(item: dict) -> RawRecord:
    authors = item.get("authors", item.get("author", []))
    if isinstance(authors, str):
        # BibTeX uses ``and`` between authors; preserve each person as one
        # value instead of splitting a person's ``family, given`` name.
        authors = [part.strip() for part in re.split(r"\s+and\s+", authors, flags=re.I)
                   if part.strip()]
    doi = item.get("doi", "")
    return RawRecord(
        source="ResearchGate",
        source_id=doi or item.get("id", item.get("url", item.get("title", ""))),
        title=item.get("title", "").strip(), authors=authors,
        venue=item.get("venue", item.get("journal", item.get("booktitle", ""))),
        abstract=item.get("abstract", item.get("description", "")),
        published_at=item.get("published_at", item.get("published", item.get("year", ""))),
        doi=doi, landing_url=item.get("landing_url", item.get("url", "")),
        oa_url=item.get("oa_url", item.get("pdf", "")),
        source_score=0.55, raw_metadata=item,
    )
