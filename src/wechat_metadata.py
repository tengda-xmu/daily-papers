"""The public metadata boundary for local WeRSS exports."""
from __future__ import annotations

import html
import re
from datetime import timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def public_records(records) -> list[dict]:
    clean = {}
    for row in records:
        if isinstance(row, RawRecord):
            row = row.to_dict()
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            continue
        url = article_url(row.get("landing_url", row.get("url", row.get("link", ""))))
        title = excerpt(row["title"])
        if not url or not title:
            continue
        date = parse_date(row.get("published_at", row.get("published", "")))
        account = row.get("venue", row.get("account", "微信公众号"))
        clean[url] = {
            "source": "微信公众号", "title": title, "account": excerpt(account),
            "published_at": date.isoformat() if date else "",
            "summary": excerpt(row.get("summary") or row.get("abstract") or row.get("description", "")),
            "landing_url": url,
        }
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
    return {"provider": "WeRSS", "exported_at": timestamp.astimezone(timezone.utc).isoformat(),
            "failed_feeds": failed if type(failed) is int and failed >= 0 else 0, "records": records}


def public_health(payload: dict) -> dict:
    """Share connector health separately, preserving the last good export."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a WeRSS health object")
    allowed = {"ok", "no_data", "quota_exhausted", "access_denied", "error"}
    timestamp = parse_date(payload.get("checked_at"))
    if payload.get("status") not in allowed or not timestamp:
        raise ValueError("Invalid WeRSS health status")
    accounts = payload.get("accounts") or []
    if not isinstance(accounts, list):
        raise ValueError("Expected a list of WeRSS account names")
    return {"provider": "WeRSS", "checked_at": timestamp.astimezone(timezone.utc).isoformat(),
            "status": payload["status"], "authenticated": payload.get("authenticated") is True,
            "accounts": [excerpt(a) for a in accounts if isinstance(a, str)][:100],
            "platform_code": "200013" if str(payload.get("platform_code")) == "200013" else ""}
