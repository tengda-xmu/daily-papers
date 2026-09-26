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
from src.wechat_metadata import excerpt, public_index_url, public_health, index_health_message
from src.wechat_subscriptions import effective_accounts, manual_queries, name_key

ROOT = Path(__file__).resolve().parents[2]
BEIJING = timezone(timedelta(hours=8))


def daily_queries(policy, accounts, until):
    """Use the same daily plan for local exports and both public columns."""
    local = until.astimezone(BEIJING)
    day = local.date().toordinal()
    pools = [
        ['机器之心 {year}年{month}月', '智能体 Skills MCP 科研'],
        ['青年编委 开放课题', '航空 企业 科研基金 申报'],
        policy.get('public_article_queries') or ['科研 会议 征稿'],
    ]
    queries = [pool[day % len(pool)].format(year=local.year, month=local.month) for pool in pools]
    targeted = manual_queries(accounts, local)
    limit = max(1, min(3, policy.get('public_daily_queries', 3)))
    if targeted:
        topics = [queries[(day + i) % len(queries)] for i in range(len(queries))]
        return list(dict.fromkeys([*targeted, *topics]))[:limit]
    targets = [f"{r['name']} {local.year}年{local.month}月" for r in accounts if r['enabled']]
    if targets:
        queries[day % 3] = targets[day % len(targets)]
    return list(dict.fromkeys(queries))[:limit]


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
                 subscribed_only=True, page=1, manual_paging=False):
        self.directory = Path(directory or ROOT / "data/inbox/wechat-subscriptions.json")
        self.cache_dir = Path(cache_dir or ROOT / "data/cache/wechat-public")
        self.queries = queries
        self.timeout = timeout
        self.subscribed_only = subscribed_only
        self.page = max(1, int(page))
        self.manual_paging = manual_paging
        self.has_more = False
        self.collection = None
        self._status = SourceStatus(self.name, "not_run")

    @property
    def status(self):
        return self._status

    def fetch(self, since, until):
        policy, names, accounts = {}, set(), []
        if self.subscribed_only or self.queries is None:
            policy = json.loads((ROOT / "config/wechat_accounts.json").read_text(encoding="utf-8"))
        if self.subscribed_only:
            accounts = effective_accounts(ROOT, self.directory)
            names = {name_key(row['name']) for row in accounts if row['enabled']}
        queries = self.queries
        if queries is None:
            queries = daily_queries(policy, accounts, until)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        records, failures, indexed_count = {}, 0, 0
        last_request, challenge, budget_exhausted = None, False, False
        now = datetime.now(timezone.utc)
        health = {'provider': 'WeChat public index', 'checked_at': now.isoformat(),
                  'planned': len(queries[:3]), 'completed': 0, 'cached': 0, 'failed': 0, 'deferred': 0, 'reasons': []}
        budget_path = self.cache_dir / "request-budget.json"
        try:
            budget = json.loads(budget_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            budget = {}
        today = now.astimezone(BEIJING).date().isoformat()
        if budget.get("day") != today:
            budget = {"day": today, "count": 0, "blocked_until": budget.get("blocked_until", "")}
        for query in queries[:3]:
            cache_query = query if self.page == 1 else f'{query}|page:{self.page}'
            key = hashlib.sha256(cache_query.encode()).hexdigest()
            path = self.cache_dir / (key + ".json")
            rows = None
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    checked = parse_date(payload.get("checked_at"))
                    if checked and timedelta(0) <= now - checked < timedelta(hours=24):
                        rows = payload["rows"]
                        health['cached'] += 1
                        self.has_more = payload.get('has_more', len(rows) >= 10)
                except (ValueError, KeyError, TypeError):
                    pass
            if rows is None:
                blocked_until = parse_date(budget.get("blocked_until"))
                if blocked_until and now < blocked_until:
                    failures += 1
                    challenge = True
                    health['deferred'] += 1
                    health['reasons'].append('verification_required')
                    continue  # Cached results remain usable during the cooldown.
                if not self.manual_paging and budget.get("count", 0) >= 3:
                    failures += 1
                    budget_exhausted = True
                    health['deferred'] += 1
                    health['reasons'].append('daily_limit')
                    continue
                if last_request is not None:
                    time.sleep(max(0, 30 - (time.monotonic() - last_request)))
                last_request = time.monotonic()
                budget["count"] = budget.get("count", 0) + 1
                budget_path.write_text(json.dumps(budget), encoding="utf-8")
                try:
                    url = "https://weixin.sogou.com/weixin?type=2&query=" + quote(query, safe="")
                    if self.page > 1:
                        url += '&page=' + str(self.page)
                    with urlopen(Request(url, headers={"User-Agent": "daily-papers/1.0"}), timeout=self.timeout) as response:
                        body = response.read().decode("utf-8", "replace")
                    rows = parse_search(body)
                    self.has_more = bool(re.search(r'id=["\']sogou_next["\']', body)) or len(rows) >= 10
                    path.write_text(json.dumps({"checked_at": now.isoformat(), "rows": rows, 'has_more': self.has_more}, ensure_ascii=False), encoding="utf-8")
                except PublicSearchChallenge:
                    failures += 1
                    health['failed'] += 1
                    health['reasons'].append('verification_required')
                    challenge = True
                    budget["blocked_until"] = (now + timedelta(hours=24)).isoformat()
                    budget_path.write_text(json.dumps(budget), encoding="utf-8")
                    continue  # Only cached results can be used for the remaining queries.
                except HTTPError as exc:
                    failures += 1
                    health['failed'] += 1
                    health['reasons'].append('verification_required' if exc.code in (403, 429) else 'http_error')
                    if exc.code in (403, 429):
                        challenge = True
                        budget["blocked_until"] = (now + timedelta(hours=24)).isoformat()
                        budget_path.write_text(json.dumps(budget), encoding="utf-8")
                except PublicSearchUnavailable:
                    failures += 1
                    health['failed'] += 1
                    health['reasons'].append('unrecognized_response')
                    continue
                except Exception:
                    failures += 1
                    health['failed'] += 1
                    health['reasons'].append('network_error')
                    continue
            if rows is None:
                continue
            health['completed'] += 1
            indexed_count += len(rows)
            for row in rows:
                if self.subscribed_only and name_key(row.get('account', '')) not in names:
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
        self.collection = public_health({**health, 'status': state})
        message += ' ' + index_health_message(self.collection)
        self._status = SourceStatus(self.name, state, len(records), message)
        return list(records.values())
