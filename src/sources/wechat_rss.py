from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from src.models import RawRecord, SourceStatus, in_date_window, parse_date
from src.wechat_metadata import excerpt, public_records, public_health, public_subscriptions, index_health_message


class WeChatRSSAdapter:
    """Read RSS, Atom or JSON exported by a self-hosted WeRSS service."""

    name = "微信公众号"

    def __init__(self, urls: list[str] | None = None,
                 headers: dict[str, str] | None = None, timeout: int = 30,
                 import_path: str | Path | None = None, merge_import: bool = False):
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
        self.merge_import = merge_import
        self.import_path = Path(import_path or os.getenv("WECHAT_IMPORT_PATH", "data/inbox/wechat.json"))
        self.health_path = self.import_path.with_name("wechat-status.json")
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self) -> SourceStatus:
        return self._status

    def fetch(self, since: datetime, until: datetime) -> list[RawRecord]:
        if self._configuration_error:
            self._status = SourceStatus(self.name, "configuration_missing", message=self._configuration_error)
            return []
        if not self.urls and not self.import_path.is_file() and not self.health_path.is_file():
            self._status = SourceStatus(
                self.name, "configuration_missing",
                message="需要配置 WECHAT_RSS_URLS，或完成本机 WeRSS 扫码、订阅并同步文章元数据。",
            )
            return []
        result, failures = [], []
        mode, timestamp, imported_failures = "WeRSS 订阅已接入", None, 0
        collection = None
        for url in self.urls:
            try:
                request = Request(url, headers=self.headers)
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8-sig")
                result.extend(self.parse(body, url))
            except Exception as exc:
                code = getattr(exc, "code", None)
                failures.append(f"HTTP {code}" if code else type(exc).__name__)
        if (not result or self.merge_import) and self.import_path.is_file():
            try:
                payload = json.loads(self.import_path.read_text(encoding="utf-8-sig"))
                if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                    raise ValueError("Invalid WeRSS export")
                has_feed_records = bool(result)
                result.extend(_item(row, "") for row in public_records(payload["records"]))
                timestamp = parse_date(payload.get("exported_at"))
                if not has_feed_records:
                    mode = "WeRSS 本地同步已接入"
                    if any(row.raw_metadata.get("access_mode") == "public_index" for row in result):
                        mode = "公开索引已接入"
                value = payload.get("failed_feeds", 0)
                imported_failures = value if type(value) is int and value > 0 else 0
                if payload.get('collection'):
                    collection = public_health(payload['collection'])
            except Exception as exc:
                failures.append(type(exc).__name__)
        # Reconstruct from a whitelist before any record reaches public data.
        imported = len(result)
        result = [_item(row, "") for row in public_records(result)]
        result = [r for r in result if in_date_window(r.published_at, since, until)]
        state = "ok" if result else "no_data"
        message = f"{mode}：读取 {imported} 条文章元数据，本期 {len(result)} 条。"
        if mode == "公开索引已接入":
            message += " 微信后台文章列表受限；已通过公开搜索获取线索，链接为检索入口，非已核验全文。"
        if timestamp:
            message += f" 最近同步：{timestamp.astimezone(timezone(timedelta(hours=8))):%m-%d %H:%M}（北京时间）。"
            now = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
            if now - timestamp > timedelta(days=3):
                state = "partial"
                message += " 导出超过 3 天，请检查同步任务；将尝试公开索引。"
            elif mode == "公开索引已接入" and now - timestamp > timedelta(hours=24):
                state = "partial"
                message += " 公开索引快照超过 24 小时，将尝试刷新。"
        elif mode == "WeRSS 本地同步已接入":
            state = "partial"
            message += " 导出缺少采集时间，请重新同步。"
        if failures or imported_failures:
            state = "partial" if result else "error"
            if failures:
                message += f" {len(failures)} 个订阅或导入请求失败；已有结果保留。"
            if imported_failures:
                message += (" 上次公开索引同步未全部完成，旧记录未保存具体原因。" if mode == '公开索引已接入' else
                            f" {imported_failures} 个订阅同步失败；已有结果保留。")
        elif not imported:
            message += " 尚无文章，请完成微信扫码并添加公众号订阅。"
        if self.health_path.is_file():
            try:
                health = public_health(json.loads(self.health_path.read_text(encoding="utf-8-sig")))
                checked = parse_date(health["checked_at"])
                if health['provider'] == 'WeChat public index':
                    if not imported and not self.urls:
                        message = '公开索引已接入：暂无已同步文章。'
                    if not collection or checked >= parse_date(collection['checked_at']):
                        collection = health
                elif (not timestamp or checked >= timestamp) and health["status"] != "ok":
                    state = health["status"]
                    message = "WeRSS 微信授权已完成；" if health["authenticated"] else "WeRSS 微信授权需要更新；"
                    accounts = health["accounts"]
                    directory = self.import_path.with_name("wechat-subscriptions.json")
                    if directory.is_file():
                        try:
                            snapshot = public_subscriptions(json.loads(directory.read_text(encoding="utf-8-sig")))
                            if parse_date(snapshot["updated_at"]) >= checked:
                                accounts = [row["name"] for row in snapshot["accounts"]]
                        except (ValueError, TypeError, OSError):
                            pass
                    message += f"已配置 {len(accounts)} 个公众号订阅。"
                    if health["status"] == "quota_exhausted":
                        message += " 微信后台列表返回 200013，已停止该接口采集；不能保证等待后恢复，将尝试公开索引。"
                    elif health["status"] == "access_denied":
                        message += " 请在本机 WeRSS 重新扫码。"
                    else:
                        message += " 本轮未取得新文章，请检查本机服务。"
                    message += f" 状态时间：{checked.astimezone(timezone(timedelta(hours=8))):%m-%d %H:%M}（北京时间）。"
                    if result:
                        message += f" 已保留 {len(result)} 条历史文章元数据。"
            except (ValueError, TypeError, OSError):
                if state in ("ok", "no_data"):
                    state = "partial" if result else "error"
                    message += " 本机同步状态文件无法读取。"
        if collection and collection['provider'] == 'WeChat public index':
            checked = parse_date(collection['checked_at'])
            if not timestamp or checked >= timestamp:
                if collection['status'] not in ('ok', 'no_data'):
                    state = 'partial' if result else collection['status']
                elif not failures and until - checked <= timedelta(hours=24):
                    state = 'ok' if result else 'no_data'
                message += ' ' + index_health_message(collection)
                message += f" 最近检查：{checked.astimezone(timezone(timedelta(hours=8))):%m-%d %H:%M}（北京时间）。"
        self._status = SourceStatus(self.name, state, len(result), message)
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
                (child for child in item if _local(child.tag) == "link"
                 and child.attrib.get("rel", "alternate") == "alternate"), None
            )
            link = ((link_node.text or "").strip() or link_node.attrib.get("href", "")) if link_node is not None else ""
            author_node = next((child for child in item if _local(child.tag) in ("author", "creator")), None)
            item_account = " ".join(author_node.itertext()).strip() if author_node is not None else account
            result.append(RawRecord(
                source="微信公众号",
                source_id=link or fields.get("guid", fields.get("id", fields.get("title", ""))),
                title=fields.get("title", ""),
                abstract=_excerpt(fields.get("description", fields.get("summary", ""))),
                published_at=fields.get("pubDate", fields.get("published", fields.get("updated", ""))),
                landing_url=link,
                venue=item_account,
                source_score=0.4,
                raw_metadata={"provider": "WeRSS", "account": item_account},
            ))
        return [record for record in result if record.title]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _excerpt(value: str) -> str:
    return excerpt(value)


def _item(item: dict, feed_url: str) -> RawRecord:
    link = item.get("landing_url", item.get("url", item.get("link", "")))
    feed = item.get("feed") if isinstance(item.get("feed"), dict) else {}
    account = item.get("account") or item.get("channel_name") or feed.get("name") or "微信公众号"
    return RawRecord(
        source="微信公众号",
        source_id=link or item.get("id", item.get("title", "")),
        title=item.get("title", ""),
        abstract=_excerpt(item.get("summary", item.get("description", ""))),
        published_at=item.get("published_at") or item.get("date_published") or item.get("published") or item.get("pubDate") or item.get("updated", ""),
        landing_url=link,
        venue=account,
        source_score=0.4,
        raw_metadata=({"provider": "Sogou WeChat public index", "account": account,
                       "access_mode": "public_index", "abstract_kind": "search_snippet",
                       "link_kind": "search_results", "account_verification": "index_label_only"}
                      if item.get("access_mode") == "public_index" else {"provider": "WeRSS", "account": account}),
    )
