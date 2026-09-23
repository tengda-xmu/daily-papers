"""Discover related WeChat accounts at low frequency, using the local WeRSS."""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from connectors.wechat_sync.export import (authenticated_session, local_api,
    WeChatRateLimited, WeChatAuthorizationRequired)
from src.models import parse_date
from src.wechat_metadata import excerpt

CONFIG = ROOT / "config/wechat_accounts.json"
STATE = ROOT / ".local/wechat-discovery.json"
SNAPSHOT = ROOT / "data/inbox/wechat-subscriptions.json"


def read_json(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def normalized(value):
    return " ".join(str(value or "").casefold().split())


def matches(term: str, text: str) -> bool:
    term = normalized(term)
    if term.isascii() and term.isalnum():
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text))
    return term in text


def account_id(fakeid):
    try:
        number = base64.b64decode(str(fakeid), validate=True).decode("ascii")
        return "MP_WXS_" + number if number.isdigit() else ""
    except (ValueError, UnicodeError):
        return ""


def classify(name: str, signature: str, policy: dict) -> list[str]:
    text = normalized(name + " " + signature)
    return [group["name"] for group in policy["groups"]
            if any(matches(term, text) for term in group["keywords"])]


def evaluate(row: dict, policy: dict, duplicate_names: set[str]) -> dict:
    name, signature = excerpt(row.get("nickname", "")), excerpt(row.get("signature", ""))
    title, text = normalized(name), normalized(name + " " + signature)
    groups = classify(name, signature, policy)
    reason = "related_research"
    if not name or not account_id(row.get("fakeid")):
        reason = "invalid_identity"
    elif title in duplicate_names:
        reason = "ambiguous_name"
    elif any(normalized(term) in text for term in policy["excluded_terms"]):
        reason = "excluded_topic"
    elif title in {normalized(n) for n in policy["seed_names"]}:
        reason = "seed_match"
    else:
        terms = {normalized(term) for group in policy["groups"] for term in group["keywords"]}
        name_matches = any(matches(term, title) for term in terms)
        matched = {term for term in terms if matches(term, text)}
        research = any(normalized(term) in normalized(signature) for term in policy["research_signals"])
        strong_research = any(term in signature for term in ("论文", "期刊", "学报", "研究", "工程", "学会"))
        if not (name_matches and (len(matched) >= 2 or strong_research) and research):
            reason = "insufficient_research_evidence"
    return {"name": name, "alias": excerpt(row.get("alias", "")), "signature": signature,
            "groups": groups, "reason": reason, "eligible": reason in {"seed_match", "related_research"}}


def manifest(feeds, state, policy, now):
    known = state.get("accounts", {})
    accounts = []
    for feed in feeds:
        info = known.get(feed["id"], {})
        accounts.append({"name": excerpt(feed.get("mp_name", "")), "alias": info.get("alias", ""),
                         "groups": info.get("groups") or classify(feed.get("mp_name", ""), feed.get("mp_intro", ""), policy),
                         "added_at": info.get("added_at", "")})
    return {"provider": "WeRSS", "updated_at": now.isoformat(), "accounts": accounts,
            "discovery": {"enabled": bool(policy["enabled"]), "status": state.get("status", "not_run"),
                          "last_run_at": state.get("last_run_at", ""),
                          "daily_queries": policy["daily_queries"], "daily_additions": policy["daily_additions"],
                          "max_subscriptions": policy["max_subscriptions"],
                          "pending_candidates": sum(1 for c in state.get("candidates", {}).values() if c.get("eligible") and not c.get("subscribed"))}}


def discover(config=CONFIG, state_path=STATE, snapshot_path=SNAPSHOT, bootstrap=False):
    policy = read_json(config, {})
    state = read_json(state_path, {"cursor": 0, "cache": {}, "accounts": {}, "candidates": {}})
    now = datetime.now(timezone.utc)
    for key in ("cache", "accounts", "candidates", "offsets"):
        state.setdefault(key, {})
    if not policy.get("enabled"):
        return {"status": "disabled", "added": []}
    next_run = parse_date(state.get("next_run_at"))
    if next_run and now < next_run:
        return {"status": "not_due", "added": []}
    try:
        token = authenticated_session()
    except Exception as exc:
        status = "access_denied" if isinstance(exc, WeChatAuthorizationRequired) else "error"
        state.update(status=status, last_run_at=now.isoformat(), next_run_at=(now + timedelta(hours=24)).isoformat())
        write_json(state_path, state)
        snapshot = read_json(snapshot_path, {"provider": "WeRSS", "accounts": [], "discovery": {}})
        snapshot["updated_at"] = now.isoformat()
        snapshot["discovery"].update(status=status, last_run_at=now.isoformat(), enabled=True)
        write_json(snapshot_path, snapshot)
        return {"status": status, "added": []}
    feeds = local_api("/mps?limit=100", token).get("list") or []
    existing = {row["id"] for row in feeds}
    added, queries_done = [], 0
    # Bootstrap is an explicit initial setup batch; routine runs stay small.
    query_budget = 10 if bootstrap else min(int(policy["daily_queries"]), 3)
    addition_budget = 20 if bootstrap else min(int(policy["daily_additions"]), 5)
    cap = min(int(policy["max_subscriptions"]), 90)
    state["status"] = "ok"
    state["last_run_at"] = now.isoformat()
    state["next_run_at"] = (now + timedelta(hours=24)).isoformat()
    last_search = None
    # Reserve the run before any external search; an interrupted process must
    # not repeatedly spend the daily request budget after restarting.
    write_json(state_path, state)

    def add_candidate(identity, candidate):
        nonlocal feeds
        if identity in existing or not candidate.get("eligible"):
            return
        if len(added) >= addition_budget or len(existing) >= cap:
            return
        if any(normalized(feed.get("mp_name")) == normalized(candidate["name"]) for feed in feeds):
            candidate.update(eligible=False, reason="existing_name_conflict")
            return
        response = local_api("/mps", token, json_body={"mp_name": candidate["name"],
            "mp_id": candidate["fakeid"], "mp_intro": candidate["signature"][:255], "fetch_articles": False})
        if response.get("id") != identity:
            raise RuntimeError("WeRSS returned an unexpected subscription identity")
        existing.add(identity)
        candidate["subscribed"] = True
        candidate["added_at"] = datetime.now(timezone.utc).isoformat()
        state["accounts"][identity] = {k: candidate[k] for k in ("name", "alias", "groups", "added_at")}
        added.append(candidate["name"])
        feeds.append({"id": identity, "mp_name": candidate["name"], "mp_intro": candidate["signature"]})
        write_json(state_path, state)
        print(json.dumps({"subscribed": candidate["name"], "groups": candidate["groups"]}, ensure_ascii=False), flush=True)

    try:
        for identity, candidate in state["candidates"].items():
            # Reapply current rules to queued candidates after policy changes.
            row = {"nickname": candidate["name"], "signature": candidate["signature"],
                   "alias": candidate["alias"], "fakeid": candidate["fakeid"]}
            duplicate_names = {normalized(candidate["name"])} if candidate.get("reason") == "ambiguous_name" else set()
            candidate.update(evaluate(row, policy, duplicate_names))
            discovered = parse_date(candidate.get("discovered_at"))
            if discovered and now - discovered < timedelta(days=policy["search_cache_days"]):
                add_candidate(identity, candidate)
        queries = policy["queries"]
        visited = 0
        while queries_done < query_budget and visited < len(queries) and len(added) < addition_budget and len(existing) < cap:
            index = int(state.get("cursor", 0)) % len(queries)
            query = queries[index]
            visited += 1
            offset = int(state["offsets"].get(query, 0))
            cache_key = query if offset == 0 else f"{query}@{offset}"
            cached = state["cache"].get(cache_key)
            fresh = cached and now - parse_date(cached["checked_at"]) < timedelta(days=policy["search_cache_days"])
            if not fresh:
                if last_search is not None:
                    time.sleep(max(0, max(30, int(policy["search_interval_seconds"])) - (time.monotonic() - last_search)))
                last_search = time.monotonic()
                response = local_api("/mps/search/" + quote(query, safe="") + f"?limit=5&offset={offset}", token)
                rows = response.get("list") or []
                cached = {"checked_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
                # Search responses contain only public account metadata.
                cached["rows"] = [{k: row.get(k, "") for k in ("nickname", "alias", "fakeid", "signature")} for row in rows]
                state["cache"][cache_key] = cached
                queries_done += 1
            rows = cached["rows"]
            duplicates = {name for name, count in Counter(normalized(r.get("nickname")) for r in rows).items() if count > 1}
            for row in rows:
                identity = account_id(row.get("fakeid"))
                candidate = {**evaluate(row, policy, duplicates), "fakeid": row.get("fakeid", ""),
                             "discovered_at": cached["checked_at"], "subscribed": identity in existing}
                state["candidates"][identity or "invalid:" + candidate["name"]] = candidate
                if identity:
                    add_candidate(identity, candidate)
            state["cursor"] = (index + 1) % len(queries)
            state["offsets"][query] = (offset + 5) % (5 * max(1, min(int(policy.get("search_pages", 3)), 3)))
            write_json(state_path, state)
        if len(existing) >= cap:
            state["status"] = "capacity_reached"
    except WeChatRateLimited:
        state["status"] = "quota_exhausted"
        state["next_run_at"] = (now + timedelta(hours=24)).isoformat()
    except WeChatAuthorizationRequired:
        state["status"] = "access_denied"
    except Exception:
        state["status"] = "error"
    finally:
        write_json(state_path, state)
        write_json(snapshot_path, manifest(feeds, state, policy, datetime.now(timezone.utc)))
    return {"status": state["status"], "added": added, "subscribed_count": len(existing), "queries": queries_done}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootstrap", action="store_true", help="Run a bounded initial expansion batch")
    args = parser.parse_args()
    try:
        result = discover(bootstrap=args.bootstrap)
    except Exception as exc:
        raise SystemExit("WeChat discovery stopped (" + type(exc).__name__ + "); inspect local authorization.") from None
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if result["status"] in {"error", "access_denied", "quota_exhausted"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
