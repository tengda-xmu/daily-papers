from __future__ import annotations

import json
import os
from datetime import datetime
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window


class WeChatRSSAdapter:
    """Read RSS, Atom or JSON exported by a self-hosted WeRSS service."""

    name = "微信公众号"

    def __init__(self, urls: list[str] | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 30):
        self.urls = urls if urls is not None else [
            value.strip() for value in os.getenv("WECHAT_RSS_URLS", "").split(",") if value.strip()
        ]
        if headers is not None:
            self.headers = headers
        else:
            raw_headers = os.getenv("WECHAT_RSS_HEADERS", "").strip()
            try:
                configured_headers = json.loads(raw_headers) if raw_headers else {}
            except json.JSONDecodeError:
                configured_headers = {}
            self.headers = {"User-Agent": "daily-papers/1.0", **configured_headers}
        self.timeout = timeout
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if not self.urls:
            self._status = SourceStatus(
                self.name, "configuration_missing",
                message="WECHAT_RSS_URLS is not configured",
            )
            return []
        result: list[RawRecord] = []
        try:
            for url in self.urls:
                request = Request(url, headers=self.headers)
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8-sig")
                result.extend(self.parse(body, url))
            result = [record for record in result
                      if in_date_window(record.published_at, since, until)]
            self._status = SourceStatus(self.name, "ok", len(result))
        except Exception as exc:
            self._status = SourceStatus(self.name, "error", len(result), str(exc))
        return result

    @staticmethod
    def parse(body: str, feed_url: str = "") -> list[RawRecord]:
        if body.lstrip().startswith(("{", "[")):
            payload = json.loads(body)
            items = payload if isinstance(payload, list) else payload.get(
                "items", payload.get("articles", []))
            return [_item(item, feed_url) for item in items
                    if isinstance(item, dict) and item.get("title")]

        root = ET.fromstring(body)
        result: list[RawRecord] = []
        for item in root.iter():
            if _local(item.tag) not in ("item", "entry"):
                continue
            fields = {
                _local(child.tag): (child.text or "").strip()
                for child in item
            }
            link_node = next(
                (child for child in item if _local(child.tag) == "link"), None
            )
            link = fields.get("link", "") or (
                link_node.attrib.get("href", "") if link_node is not None else ""
            )
            result.append(RawRecord(
                source="微信公众号",
                source_id=link or fields.get("guid", fields.get("id", fields.get("title", ""))),
                title=fields.get("title", ""),
                abstract=fields.get("description", fields.get("summary", "")),
                published_at=fields.get("pubDate", fields.get("published", fields.get("updated", ""))),
                landing_url=link,
                venue="微信公众号",
                source_score=0.4,
                raw_metadata={"feed_url": feed_url, "item": fields},
            ))
        return [record for record in result if record.title]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _item(item: dict, feed_url: str) -> RawRecord:
    link = item.get("url", item.get("link", ""))
    return RawRecord(
        source="微信公众号",
        source_id=link or item.get("id", item.get("title", "")),
        title=item.get("title", ""),
        abstract=item.get("summary", item.get("description", "")),
        published_at=item.get("published", item.get("pubDate", "")),
        landing_url=link,
        venue=item.get("account", "微信公众号"),
        source_score=0.4,
        raw_metadata={"feed_url": feed_url, **item},
    )
