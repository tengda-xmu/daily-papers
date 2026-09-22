from __future__ import annotations

import json
import os
import html
import re
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
        self._configuration_error = ""
        if headers is not None:
            self.headers = headers
        else:
            raw_headers = os.getenv("WECHAT_RSS_HEADERS", "").strip()
            try:
                configured_headers = json.loads(raw_headers) if raw_headers else {}
                if not isinstance(configured_headers, dict) or any(
                    not isinstance(k, str) or not isinstance(v, str) for k, v in configured_headers.items()
                ):
                    raise ValueError("Headers must be a string mapping")
            except (ValueError, TypeError):
                configured_headers = {}
                self._configuration_error = "WECHAT_RSS_HEADERS must be a JSON object with string values"
            self.headers = {"User-Agent": "daily-papers/1.0", **configured_headers}
        self.timeout = timeout
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if self._configuration_error:
            self._status = SourceStatus(self.name, "configuration_missing", message=self._configuration_error)
            return []
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
            # Exceptions can contain the private feed URL. Publish only an
            # error type/code, never authentication parameters.
            code = getattr(exc, "code", None)
            self._status = SourceStatus(self.name, "error", len(result),
                                        f"HTTP {code}" if code else type(exc).__name__)
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
        account = root.findtext("./channel/title") or root.findtext("{*}title") or "微信公众号"
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
                abstract=_excerpt(fields.get("description", fields.get("summary", ""))),
                published_at=fields.get("pubDate", fields.get("published", fields.get("updated", ""))),
                landing_url=link,
                venue=account,
                source_score=0.4,
                raw_metadata={"provider": "WeRSS", "account": account},
            ))
        return [record for record in result if record.title]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _excerpt(value: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    text = " ".join(text.split())
    return text[:300] + ("…" if len(text) > 300 else "")


def _item(item: dict, feed_url: str) -> RawRecord:
    link = item.get("url", item.get("link", ""))
    return RawRecord(
        source="微信公众号",
        source_id=link or item.get("id", item.get("title", "")),
        title=item.get("title", ""),
        abstract=_excerpt(item.get("summary", item.get("description", ""))),
        published_at=item.get("published", item.get("pubDate", "")),
        landing_url=link,
        venue=item.get("account", "微信公众号"),
        source_score=0.4,
        raw_metadata={"provider": "WeRSS", "account": item.get("account", "微信公众号")},
    )
