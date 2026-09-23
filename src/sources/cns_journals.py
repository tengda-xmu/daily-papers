"""Bounded, journal-specific Crossref queries for CNS family recommendations."""
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

from src.models import SourceStatus
from src.venues import classify_venue
from src.sources.public_literature import CrossrefAdapter

CONFIG = Path(__file__).resolve().parents[2] / "config/cns-search.json"


class CNSJournalAdapter(CrossrefAdapter):
    name = "CNS 子刊专项"

    def __init__(self, config=None, **kwargs):
        super().__init__(**kwargs)
        self.config = config if config is not None else json.loads(CONFIG.read_text(encoding="utf-8"))

    def fetch(self, since, until):
        try:
            days = max(1, int(os.getenv("CNS_LOOKBACK_DAYS") or self.config["lookback_days"]))
        except (ValueError, KeyError):
            days = 180
        start = until - timedelta(days=days)
        records, failures, completed = [], [], 0
        journals = self.config["journals"]
        for index, journal in enumerate(journals):
            if index:
                time.sleep(2)
            params = {"filter": f"issn:{journal['issn']},from-pub-date:{start.date()},until-pub-date:{until.date()}",
                      "query": self.config["query"], "rows": 30}
            url = "https://api.crossref.org/works?" + urlencode(params)
            headers = {"User-Agent": "daily-papers/1.0 (https://github.com/tengda-xmu/daily-papers)"}
            try:
                try:
                    payload = self._get_json(url, headers)
                except HTTPError as exc:
                    delay = exc.headers.get("Retry-After", "10") if exc.headers else "10"
                    if exc.code != 429 or not delay.isdigit() or int(delay) > 30:
                        raise
                    time.sleep(max(10, int(delay)))
                    payload = self._get_json(url, headers)
                for record in self.parse_payload(payload):
                    if classify_venue(record.venue) != "CNS 子刊":
                        continue
                    record.raw_metadata.update(query_scope=self.name, sources=["Crossref", self.name])
                    records.append(record)
                completed += 1
            except Exception as exc:
                code = getattr(exc, "code", None)
                failures.append(f"{journal['name']}: HTTP {code}" if code else f"{journal['name']}: {type(exc).__name__}")
                if code == 429:
                    break
        records = self._finish(records, start, until)
        if failures:
            self._status = SourceStatus(self.name, "partial" if records else "error", len(records),
                                        f"{completed}/{len(journals)} journals; " + "; ".join(failures))
        else:
            self._status.message = f"Crossref ISSN queries: {completed} journals; window {days} days"
        return records
