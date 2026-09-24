"""Low-frequency public article discovery; never calls WeChat's private list API."""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.models import RawRecord, SourceStatus, in_date_window, parse_date
from src.wechat_metadata import excerpt, public_index_url

ROOT = Path(__file__).resolve().parents[2]


class PublicSearchUnavailable(RuntimeError):
    pass


class PublicSearchChallenge(PublicSearchUnavailable):
    pass


class SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.current, self.field = [], None, None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "li" and attrs.get("id", "").startswith("sogou_vr_"):
            self.current = {"title": "", "summary": "", "account": "", "published_at": ""}
        if self.current is not None:
            if tag == "h3":
                self.field = "title"
            elif tag == "p" and "txt-info" in attrs.get("class", "").split():
                self.field = "summary"
            elif tag == "span" and "all-time-y2" in attrs.get("class", "").split():
                self.field = "account"

    def handle_endtag(self, tag):
        if (tag, self.field) in (("h3", "title"), ("p", "summary"), ("span", "account")):
            self.field = None
        if tag == "li" and self.current is not None:
            self.rows.append(self.current)
            self.current, self.field = None, None

    def handle_data(self, value):
        if self.current is None:
            return
        if self.field:
            self.current[self.field] += value
        match = re.search(r"timeConvert\(['\"]([0-9]{9,11})['\"]\)", value)
        if match:
            try:
                self.current["published_at"] = datetime.fromtimestamp(int(match.group(1)), timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass


def parse_search(body: str) -> list[dict]:
    if any(marker in body for marker in ("antispider", "此验证码用于", "请输入验证码", "访问过于频繁")):
        raise PublicSearchChallenge("Public search requires human verification")
    if 'class="news-list"' not in body and not any(text in body for text in ("没有找到相关", "未找到相关", "未搜索到相关")):
        raise PublicSearchUnavailable("No recognizable public article results")
    parser = SearchParser()
    parser.feed(body)
    return [{key: excerpt(value) for key, value in row.items()} for row in parser.rows
            if row["title"].strip() and row["account"].strip() and row["published_at"]]


class WeChatPublicIndexAdapter:
    name = "微信公众号"

    def __init__(self, directory=None, cache_dir=None, queries=None, timeout=25,
                 subscribed_only=True):
        self.directory = Path(directory or ROOT / "data/inbox/wechat-subscriptions.json")
        self.cache_dir = Path(cache_dir or ROOT / "data/cache/wechat-public")
        self.queries = queries
        self.timeout = timeout
        self.subscribed_only = subscribed_only
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self):
        return self._status

    def fetch(self, since, until):
        policy, names = {}, set()
        if self.subscribed_only or self.queries is None:
            policy = json.loads((ROOT / "config/wechat_accounts.json").read_text(encoding="utf-8"))
        if self.subscribed_only:
            snapshot = json.loads(self.directory.read_text(encoding="utf-8-sig")) if self.directory.is_file() else {}
            names = {row["name"] for row in snapshot.get("accounts", [])} or set(policy["seed_names"])
        queries = self.queries
        if queries is None:
            pool = policy["public_article_queries"]
            offset = (until.date().toordinal() % len(pool))
            queries = [pool[(offset + i) % len(pool)].format(year=until.year, month=until.month)
                       for i in range(min(3, policy.get("public_daily_queries", 3)))]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        records, failures, indexed_count = {}, 0, 0
        last_request, challenge, budget_exhausted = None, False, False
        now = datetime.now(timezone.utc)
        budget_path = self.cache_dir / "request-budget.json"
        try:
            budget = json.loads(budget_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            budget = {}
        if budget.get("day") != now.date().isoformat():
            budget = {"day": now.date().isoformat(), "count": 0, "blocked_until": budget.get("blocked_until", "")}
        for query in queries[:3]:
            key = hashlib.sha256(query.encode()).hexdigest()
            path = self.cache_dir / (key + ".json")
            rows = None
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    checked = parse_date(payload.get("checked_at"))
                    if checked and timedelta(0) <= now - checked < timedelta(hours=24):
                        rows = payload["rows"]
                except (ValueError, KeyError, TypeError):
                    pass
            if rows is None:
                blocked_until = parse_date(budget.get("blocked_until"))
                if blocked_until and now < blocked_until:
                    failures += 1
                    challenge = True
                    continue  # Cached results remain usable during the cooldown.
                if budget.get("count", 0) >= 3:
                    failures += 1
                    budget_exhausted = True
                    continue
                if last_request is not None:
                    time.sleep(max(0, 30 - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                budget["count"] = budget.get("count", 0) + 1
                budget_path.write_text(json.dumps(budget), encoding="utf-8")
                try:
                    url = "https://weixin.sogou.com/weixin?type=2&query=" + quote(query, safe="")
                    with urlopen(Request(url, headers={"User-Agent": "daily-papers/1.0"}), timeout=self.timeout) as response:
                        body = response.read().decode("utf-8", "replace")
                    rows = parse_search(body)
                    path.write_text(json.dumps({"checked_at": now.isoformat(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
                except PublicSearchChallenge:
                    failures += 1
                    challenge = True
                    budget["blocked_until"] = (now + timedelta(hours=24)).isoformat()
                    budget_path.write_text(json.dumps(budget), encoding="utf-8")
                    continue  # Only cached results can be used for the remaining queries.
                except HTTPError as exc:
                    failures += 1
                    if exc.code in (403, 429):
                        challenge = True
                        budget["blocked_until"] = (now + timedelta(hours=24)).isoformat()
                        budget_path.write_text(json.dumps(budget), encoding="utf-8")
                except Exception:
                    failures += 1
                    continue
            if rows is None:
                continue
            indexed_count += len(rows)
            for row in rows:
                if self.subscribed_only and row.get("account") not in names:
                    continue
                if not in_date_window(row.get("published_at", ""), since, until):
                    continue
                title, account = row["title"], row["account"]
                identity = hashlib.sha256((account + "|" + title + "|" + row["published_at"]).encode()).hexdigest()[:20]
                records[identity] = RawRecord(source=self.name, source_id="wechat-index:" + identity,
                    title=title, authors=[], venue=account, abstract=excerpt(row.get("summary", "")),
                    published_at=row["published_at"], landing_url=public_index_url(account, title), source_score=0.4,
                    raw_metadata={"provider": "Sogou WeChat public index", "access_mode": "public_index",
                                  "abstract_kind": "search_snippet", "link_kind": "search_results",
                                  "account_verification": "index_label_only"})
        state = ("partial" if failures else "ok") if records else (
            "access_denied" if challenge else "quota_exhausted" if budget_exhausted else "error" if failures else "no_data")
        if self.subscribed_only:
            message = f"公开索引已接入：本期读取 {len(records)} 条公众号文章线索，按 {len(names)} 个订阅筛选。"
            message += " 微信后台文章列表受限；线索提供公开检索入口，非已核验全文。"
        else:
            message = f"公开索引返回 {indexed_count} 条线索，所选日期内 {len(records)} 条；不限已订阅公众号。"
            message += " 结果为索引片段，提供公开检索入口；不代表公众号全量历史。"
        if failures:
            message += " 部分公开查询失败，已保留成功结果。"
        if challenge:
            message += " 公开搜索访问受限或要求人工验证，已停止继续请求。"
        if budget_exhausted:
            message += " 当日公开检索预算已用完，缓存结果保留，次日继续。"
        self._status = SourceStatus(self.name, state, len(records), message)
        return list(records.values())
