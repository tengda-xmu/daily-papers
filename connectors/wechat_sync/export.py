"""Export public article metadata from the owner's local WeRSS instance."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.settings import load_env, read_env
from src.sources.wechat_rss import WeChatRSSAdapter
from src.wechat_metadata import public_export, public_health

LOCAL_API = "http://127.0.0.1:8001/api/v1/wx"


class WeChatRateLimited(RuntimeError):
    pass


class WeChatAuthorizationRequired(RuntimeError):
    pass


def save_health(status: str, authenticated: bool, accounts: list[str], output: Path):
    payload = public_health({"checked_at": datetime.now(timezone.utc).isoformat(), "status": status,
                             "authenticated": authenticated, "accounts": accounts,
                             "platform_code": "200013" if status == "quota_exhausted" else ""})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def local_api(path: str, token: str = "", form: dict | None = None, timeout: int = 45):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = urlencode(form).encode() if form is not None else None
    request = Request(LOCAL_API + path, data=data, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        details = str(payload.get("data", {}))
        if "200013" in details:
            raise WeChatRateLimited("WeChat rate limited this collection")
        if payload.get("code") == 40101:
            raise WeChatAuthorizationRequired("WeChat authorization expired")
        raise RuntimeError("WeRSS rejected the request; inspect the local service")
    return payload.get("data") or {}


def refresh_local_feeds(health_path: Path | None = None) -> int:
    """Use the local service's credentials, never upload them to GitHub."""
    env = read_env(ROOT / ".local/werss-source/.env")
    if not all(env.get(key) for key in ("WERSS_USERNAME", "WERSS_PASSWORD")):
        raise RuntimeError("Local WeRSS service credentials are missing")
    login = local_api("/auth/login", form={"username": env["WERSS_USERNAME"], "password": env["WERSS_PASSWORD"]})
    token = login["access_token"]
    feeds = local_api("/mps?limit=100", token).get("list") or []
    names = [str(feed.get("mp_name", "")) for feed in feeds]
    # The local server restores saved authorization in a startup thread.
    # Poll only the local state, allowing that thread to finish first.
    authenticated = False
    for attempt in range(7):
        authenticated = bool(local_api("/auth/qr/status", token).get("login_status"))
        if authenticated:
            break
        if attempt < 6:
            time.sleep(5)
    if not authenticated:
        if health_path:
            save_health("access_denied", False, names, health_path)
        raise WeChatAuthorizationRequired("Complete the WeRSS WeChat QR authorization first")
    if not feeds:
        raise RuntimeError("Add WeChat accounts in WeRSS before synchronizing")
    failures = 0
    for feed in feeds:
        try:
            result = local_api("/mps/update/" + quote(str(feed["id"]), safe=""), token, timeout=180)
            if result.get("status") == "processing":
                # A background task is not a completed metadata collection.
                # Preserve the previous export rather than dating it as new.
                raise RuntimeError("WeRSS collection is still running")
        except WeChatRateLimited:
            if health_path:
                save_health("quota_exhausted", True, names, health_path)
            raise  # Stop all remaining accounts on a platform rate limit.
        except WeChatAuthorizationRequired:
            if health_path:
                save_health("access_denied", False, names, health_path)
            raise
        except Exception:
            failures += 1
    if failures == len(feeds):
        if health_path:
            save_health("error", True, names, health_path)
        raise RuntimeError("All subscriptions failed to refresh; previous export preserved")
    if health_path:
        save_health("ok", True, names, health_path)
    return failures


def export(output: Path, refresh: bool = False) -> int:
    load_env()
    health_path = output.with_name("wechat-status.json")
    failures = refresh_local_feeds(health_path) if refresh else 0
    urls = [u.strip() for u in os.getenv("WECHAT_RSS_URLS", "").split(",") if u.strip()]
    if not urls:
        urls = ["http://127.0.0.1:8001/feed/all.json?limit=100"]
    adapter = WeChatRSSAdapter(urls=urls, import_path=ROOT / ".local/no-wechat-fallback.json")
    now = datetime.now(timezone.utc)
    records = adapter.fetch(now - timedelta(days=30), now)
    if adapter.status.status not in ("ok", "partial") or not records:
        raise RuntimeError("No current WeRSS article metadata; authorize WeChat and add subscriptions")
    payload = public_export({"exported_at": now.isoformat(), "failed_feeds": failures + (adapter.status.status == "partial"),
                             "records": records})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return len(payload["records"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Refresh subscribed accounts in the local WeRSS service first")
    parser.add_argument("--output", type=Path, default=ROOT / "data/inbox/wechat.json")
    args = parser.parse_args()
    try:
        count = export(args.output, args.refresh)
    except Exception as exc:
        # Network exceptions may contain feed credentials. Do not print them.
        raise SystemExit(f"WeChat export stopped ({type(exc).__name__}); check local WeRSS authorization and subscriptions. Previous export preserved.") from None
    print(f"Exported {count} WeChat article metadata records; no full text or sessions included.")


if __name__ == "__main__":
    main()
