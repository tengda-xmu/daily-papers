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
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from src.settings import load_env, read_env
from src.sources.wechat_rss import WeChatRSSAdapter
from src.sources.wechat_public_index import WeChatPublicIndexAdapter
from src.wechat_metadata import public_export, public_health

LOCAL_API = "http://127.0.0.1:8001/api/v1/wx"


class WeChatRateLimited(RuntimeError):
    pass


class WeChatAuthorizationRequired(RuntimeError):
    pass


class WeChatCollectionPending(RuntimeError):
    pass


def save_health(status: str, authenticated: bool, accounts: list[str], output: Path):
    payload = public_health({"checked_at": datetime.now(timezone.utc).isoformat(), "status": status,
                             "authenticated": authenticated, "accounts": accounts,
                             "platform_code": "200013" if status == "quota_exhausted" else ""})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def local_api(path: str, token: str = "", form: dict | None = None, timeout: int = 45,
              json_body: dict | None = None):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = urlencode(form).encode() if form is not None else None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    request = Request(LOCAL_API + path, data=data, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            payload = json.load(exc)
        except (ValueError, OSError):
            raise RuntimeError("Local WeRSS request failed") from None
    if isinstance(payload.get("detail"), dict):
        payload = payload["detail"]
    if payload.get("code") != 0:
        details = str(payload.get("data", {}))
        if "200013" in details:
            raise WeChatRateLimited("WeChat rate limited this collection")
        if payload.get("code") == 40101 or "Invalid Session" in details:
            raise WeChatAuthorizationRequired("WeChat authorization expired")
        raise RuntimeError("WeRSS rejected the request; inspect the local service")
    return payload.get("data") or {}


def authenticated_session() -> str:
    env = read_env(ROOT / ".local/werss-source/.env")
    if not all(env.get(key) for key in ("WERSS_USERNAME", "WERSS_PASSWORD")):
        raise RuntimeError("Local WeRSS service credentials are missing")
    token = local_api("/auth/login", form={"username": env["WERSS_USERNAME"], "password": env["WERSS_PASSWORD"]})["access_token"]
    for attempt in range(7):
        if local_api("/auth/qr/status", token).get("login_status"):
            return token
        if attempt < 6:
            time.sleep(5)
    raise WeChatAuthorizationRequired("Complete the WeRSS WeChat QR authorization first")


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
    policy_path = ROOT / "config/wechat_accounts.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.is_file() else {}
    budget = max(1, min(int(policy.get("max_daily_article_feeds", 12)), 12))
    # Oldest attempted subscriptions go first, so newly added accounts do
    # not cause an unbounded morning run or starve existing subscriptions.
    selected = sorted(feeds, key=lambda feed: (int(feed.get("sync_time") or 0), str(feed["id"])))[:budget]
    deadline = time.monotonic() + 15 * 60
    failures = 0
    attempted = 0
    for feed in selected:
        if time.monotonic() + 180 > deadline:
            break
        attempted += 1
        try:
            result = local_api("/mps/update/" + quote(str(feed["id"]), safe=""), token, timeout=180)
            if result.get("status") == "processing":
                # A background task is not a completed metadata collection.
                # Preserve the previous export rather than dating it as new.
                raise WeChatCollectionPending("WeRSS collection is still running")
        except WeChatRateLimited:
            if health_path:
                save_health("quota_exhausted", True, names, health_path)
            raise  # Stop all remaining accounts on a platform rate limit.
        except WeChatAuthorizationRequired:
            if health_path:
                save_health("access_denied", False, names, health_path)
            raise
        except (WeChatCollectionPending, TimeoutError):
            if health_path:
                save_health("error", True, names, health_path)
            raise  # Do not start another collection alongside a pending one.
        except Exception:
            failures += 1
    if failures == attempted:
        if health_path:
            save_health("error", True, names, health_path)
        raise RuntimeError("All subscriptions failed to refresh; previous export preserved")
    if health_path:
        save_health("ok", True, names, health_path)
    return failures


def export(output: Path, refresh: bool = False) -> int:
    load_env()
    health_path = output.with_name("wechat-status.json")
    policy_path = ROOT / "config/wechat_accounts.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8")) if policy_path.is_file() else {}
    use_index = refresh and policy.get("article_mode") == "public_index"
    failures = refresh_local_feeds(health_path) if refresh and not use_index else 0
    urls = [u.strip() for u in os.getenv("WECHAT_RSS_URLS", "").split(",") if u.strip()]
    if not urls:
        urls = ["http://127.0.0.1:8001/feed/all.json?limit=100"]
    adapter = WeChatPublicIndexAdapter() if use_index else WeChatRSSAdapter(urls=urls, import_path=ROOT / ".local/no-wechat-fallback.json")
    now = datetime.now(timezone.utc)
    records = adapter.fetch(now - timedelta(days=30), now)
    if adapter.status.status not in ("ok", "partial") or not records:
        raise RuntimeError("No new public index metadata; previous export preserved" if use_index else
                           "No current WeRSS article metadata; check the local service")
    if use_index and output.is_file():
        from src.wechat_metadata import public_records
        from src.models import in_date_window
        try:
            old = json.loads(output.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError):
            old = {}
        preserved = [row for row in public_records(old.get("records", []))
                     if in_date_window(row.get("published_at", ""), now - timedelta(days=30), now)]
        records = [*preserved, *records]
    payload = public_export({"exported_at": now.isoformat(), "failed_feeds": failures + int(adapter.status.status == "partial"),
                             "records": records})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)
    return len(payload["records"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Collect recent metadata using the configured article_mode")
    parser.add_argument("--output", type=Path, default=ROOT / "data/inbox/wechat.json")
    args = parser.parse_args()
    try:
        count = export(args.output, args.refresh)
    except Exception as exc:
        # Network exceptions may contain feed credentials. Do not print them.
        raise SystemExit(f"WeChat export stopped ({type(exc).__name__}); no new usable metadata. Check the configured article source. Previous export preserved.") from None
    print(f"Exported {count} WeChat article metadata records; no full text or sessions included.")


if __name__ == "__main__":
    main()
