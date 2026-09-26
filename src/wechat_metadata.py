"""The public metadata boundary for local WeRSS exports."""
from __future__ import annotations

import html
import re
from datetime import timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote

from src.models import RawRecord, parse_date


def article_url(value: str) -> str:
    try:
        url = urlsplit(str(value or ""))
        if (url.scheme not in ("https", "http") or url.hostname != "mp.weixin.qq.com"
                or url.username or url.password or url.port not in (None, 80, 443)):
            return ""
        if re.fullmatch(r"/s/[A-Za-z0-9_-]+", url.path):
            query = ""
        elif url.path == "/s":
            fields = dict(parse_qsl(url.query))
            if not all(fields.get(key) for key in ("__biz", "mid", "idx", "sn")):
                return ""
            query = urlencode({key: fields[key] for key in ("__biz", "mid", "idx", "sn")})
        else:
            return ""
        return urlunsplit(("https", "mp.weixin.qq.com", url.path, query, ""))
    except ValueError:
        return ""


def excerpt(value: str) -> str:
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", str(value or ""), flags=re.S | re.I)
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return text[:300] + ("…" if len(text) > 300 else "")


def public_index_url(account: str, title: str) -> str:
    # Rebuild a stable public search URL, never retain expiring redirect tokens.
    return "https://weixin.sogou.com/weixin?type=2&query=" + quote(excerpt(account) + " " + excerpt(title), safe="")


def public_records(records) -> list[dict]:
    clean = {}
    for row in records:
        if isinstance(row, RawRecord):
            row = row.to_dict()
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        url = article_url(row.get("landing_url", row.get("url", row.get("link", ""))))
        title = excerpt(row["title"])
        metadata = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
        indexed = row.get("access_mode") == "public_index" or metadata.get("access_mode") == "public_index"
        account = row.get("venue", row.get("account", "微信公众号"))
        if indexed:
            url = public_index_url(account, title)
        if not url or not title:
            continue
        date = parse_date(row.get("published_at", row.get("published", "")))
        clean[url] = {
            "source": "微信公众号", "title": title, "account": excerpt(account),
            "published_at": date.isoformat() if date else "",
            "summary": excerpt(row.get("summary") or row.get("abstract") or row.get("description", "")),
            "landing_url": url,
        }
        if indexed:
            clean[url]["access_mode"] = "public_index"
            clean[url]["link_kind"] = "search_results"
    return list(clean.values())


def public_export(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected a WeRSS metadata export")
    records = public_records(payload.get("records", []))
    if not records:
        raise ValueError("No valid WeChat article metadata; previous export preserved")
    timestamp = parse_date(payload.get("exported_at"))
    if not timestamp:
        raise ValueError("Collection timestamp is required")
    failed = payload.get("failed_feeds", 0)
    indexed = any(row.get("access_mode") == "public_index" for row in records)
    result = {"provider": "WeChat public index" if indexed else "WeRSS", "exported_at": timestamp.astimezone(timezone.utc).isoformat(),
            "failed_feeds": failed if type(failed) is int and failed >= 0 else 0, "records": records}
    if payload.get('collection'):
        result['collection'] = public_health(payload['collection'])
    return result


def public_health(payload: dict) -> dict:
    """Share connector health separately, preserving the last good export."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a WeRSS health object")
    allowed = {"ok", "no_data", "quota_exhausted", "access_denied", "error"}
    indexed = payload.get('provider') == 'WeChat public index'
    if indexed:
        allowed.add('partial')
    timestamp = parse_date(payload.get("checked_at"))
    if payload.get("status") not in allowed or not timestamp:
        raise ValueError("Invalid WeRSS health status")
    if indexed:
        result = {'provider': 'WeChat public index', 'checked_at': timestamp.astimezone(timezone.utc).isoformat(),
                  'status': payload['status']}
        for field in ('planned', 'completed', 'cached', 'failed', 'deferred'):
            value = payload.get(field, 0)
            result[field] = min(value, 3) if type(value) is int and value >= 0 else 0
        reasons = payload.get('reasons', [])
        result['reasons'] = sorted({r for r in reasons if isinstance(r, str) and r in INDEX_REASONS}) if isinstance(reasons, list) else []
        return result
    accounts = payload.get("accounts") or []
    if not isinstance(accounts, list):
        raise ValueError("Expected a list of WeRSS account names")
    return {"provider": "WeRSS", "checked_at": timestamp.astimezone(timezone.utc).isoformat(),
            "status": payload["status"], "authenticated": payload.get("authenticated") is True,
            "accounts": [excerpt(a) for a in accounts if isinstance(a, str)][:100],
            "platform_code": "200013" if str(payload.get("platform_code")) == "200013" else ""}


INDEX_REASONS = {
    'daily_limit': '当日公开检索预算已用完；保留缓存，次日继续',
    'verification_required': '公开搜索访问受限或要求人工验证；已停止新请求并进入 24 小时冷却',
    'http_error': '公开搜索服务返回 HTTP 错误',
    'network_error': '公开搜索连接失败或超时',
    'unrecognized_response': '公开搜索未返回可识别的文章列表',
}


def index_health_message(health):
    health = public_health(health)
    text = f"公开查询完成 {health['completed']}/{health['planned']} 项（复用缓存 {health['cached']} 项）。"
    if health['failed']:
        text += f" {health['failed']} 项失败。"
    if health['deferred']:
        text += f" {health['deferred']} 项暂缓。"
    for reason in health['reasons']:
        text += ' ' + INDEX_REASONS[reason] + '。'
    return text


def public_subscriptions(payload: dict) -> dict:
    """Expose the subscription directory, never search caches or sessions."""
    if not isinstance(payload, dict) or not parse_date(payload.get("updated_at")):
        raise ValueError("Invalid WeRSS subscription snapshot")
    if not isinstance(payload.get("accounts"), list):
        raise ValueError("Missing WeRSS subscription accounts")
    accounts = []
    for row in payload["accounts"][:100]:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        groups = row.get("groups") if isinstance(row.get("groups"), list) else []
        added = parse_date(row.get("added_at"))
        accounts.append({"name": excerpt(row["name"]), "alias": excerpt(row.get("alias", "")),
                         "groups": [excerpt(group) for group in groups if isinstance(group, str)][:10],
                         "added_at": added.isoformat() if added else ""})
    discovery = payload.get("discovery") or {}
    if not isinstance(discovery, dict):
        raise ValueError("Invalid discovery status")
    status = discovery.get("status", "not_run")
    if status not in {"ok", "not_run", "disabled", "quota_exhausted", "access_denied", "error", "capacity_reached"}:
        raise ValueError("Invalid discovery status")
    last_run = parse_date(discovery.get("last_run_at"))
    safe_discovery = {"enabled": discovery.get("enabled") is True, "status": status,
                      "last_run_at": last_run.isoformat() if last_run else ""}
    for field, maximum in (("daily_queries", 3), ("daily_additions", 5), ("max_subscriptions", 90), ("pending_candidates", 10000)):
        value = discovery.get(field, 0)
        safe_discovery[field] = min(value, maximum) if type(value) is int and value >= 0 else 0
    return {"provider": "WeRSS", "updated_at": parse_date(payload["updated_at"]).isoformat(),
            "accounts": accounts, "discovery": safe_discovery}
