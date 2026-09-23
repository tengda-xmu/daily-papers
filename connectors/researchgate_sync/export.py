"""Low-frequency ResearchGate metadata exporter.

The first run is interactive: Playwright opens a browser so the owner can log
in. The saved browser profile is local-only and is never committed. For
repeatable CI ingestion this script writes a small metadata JSON file that the
main pipeline can import.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.settings import load_env
from src.models import normalize_doi
from tools.publish_researchgate import public_records


def researchgate_url(value: str, publication_only: bool = False) -> str:
    parsed = urlsplit(str(value or ""))
    paths = r"/publication/\d+[^/?#]*" if publication_only else r"/(?:profile/[^/?#]+|publication/\d+[^/?#]*)"
    if (parsed.scheme != "https" or parsed.hostname not in ("www.researchgate.net", "researchgate.net")
            or parsed.username or parsed.password or not re.fullmatch(paths, parsed.path.rstrip("/"))):
        raise ValueError("Use an HTTPS ResearchGate profile or publication URL")
    return urlunsplit(("https", "www.researchgate.net", parsed.path.rstrip("/"), "", ""))


def normalize_records(rows: list[dict]) -> list[dict]:
    records = {}
    for row in rows:
        try:
            url = researchgate_url(row.get("landing_url", ""), publication_only=True)
        except (ValueError, TypeError):
            continue
        title = " ".join(str(row.get("title", "")).split())
        if len(title) < 8 or title.casefold() in ("view publication", "download full-text", "request full-text"):
            continue
        authors = row.get("authors") or []
        authors = _authors(authors) if isinstance(authors, str) else authors
        record = {"source": "ResearchGate", "title": title, "authors": authors,
                  "published_at": publication_date(row.get("published_at", "")),
                  "venue": str(row.get("venue") or "").strip(),
                  "doi": normalize_doi(row.get("doi") or _doi(str(row.get("text", "")))),
                  "landing_url": url}
        key = re.search(r"/publication/(\d+)", url).group(1)
        previous = records.get(key)
        if previous:
            for field in ("authors", "published_at", "venue", "doi"):
                if not record[field]:
                    record[field] = previous[field]
            if len(previous["title"]) > len(title):
                record["title"] = previous["title"]
        records[key] = record
    return public_records(list(records.values())) if records else []


def collect_page(page) -> list[dict]:
    # Only call on configured public profile/publication pages, never a feed,
    # messages page, login form or arbitrary account page.
    researchgate_url(page.url)
    script = Path(__file__).with_name("read_publications.js").read_text(encoding="utf-8")
    return normalize_records(page.evaluate(script))


def publication_date(value: str) -> str:
    text = str(value or "").strip()
    iso = re.search(r"\b((?:19|20)\d{2})(?:[-/.](\d{1,2})(?:[-/.](\d{1,2}))?)?\b", text)
    if iso and iso.group(2):
        try:
            year, month, day = int(iso[1]), int(iso[2]), int(iso[3] or 1)
            datetime(year, month, day)
            return f"{year:04d}-{month:02d}" + (f"-{day:02d}" if iso[3] else "")
        except ValueError:
            return ""
    months = {name: i for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
    match = re.search(r"\b([A-Za-z]{3,9})\s+((?:19|20)\d{2})\b", text)
    if match and match[1][:3].lower() in months:
        return f"{match[2]}-{months[match[1][:3].lower()]:02d}"
    return _year(text)


def save_export(records: list[dict], output: Path, failed_pages: int = 0) -> None:
    records = normalize_records(records)
    if not records:
        raise ValueError("No valid publication metadata; previous export preserved")
    payload = {"exported_at": datetime.now(timezone.utc).isoformat(),
               "failed_pages": failed_pages, "records": records}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)


def main():
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", action="append", dest="urls", default=[])
    parser.add_argument("--output", default=os.getenv("RESEARCHGATE_IMPORT_PATH", "data/inbox/researchgate.json"))
    parser.add_argument("--session", default=os.getenv("RESEARCHGATE_SESSION_DIR", ".local/researchgate"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--login", action="store_true", help="Wait for manual login before collecting")
    parser.add_argument("--channel", default=os.getenv("RESEARCHGATE_BROWSER_CHANNEL", ""), help="Use msedge/chrome or installed Playwright Chromium")
    args = parser.parse_args()
    if args.login and args.headless:
        parser.error("--login requires a visible browser; omit --headless")
    urls = args.urls or [x.strip() for x in os.getenv("RESEARCHGATE_AUTHOR_URLS", "").split(",") if x.strip()]
    if not urls:
        raise SystemExit("Provide --url or RESEARCHGATE_AUTHOR_URLS")
    urls = list(dict.fromkeys(researchgate_url(url) for url in urls))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install connector dependencies with: pip install -r connectors/researchgate_sync/requirements.txt") from exc
    records, failed_pages = [], 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(args.session, headless=args.headless, channel=args.channel or None)
        page = browser.pages[0] if browser.pages else browser.new_page()
        if args.login:
            page.goto("https://www.researchgate.net/login", wait_until="domcontentloaded", timeout=60000)
            input("Complete ResearchGate login in the browser, then press Enter here to collect metadata: ")
        for target in urls:
            try:
                response = page.goto(target, wait_until="domcontentloaded", timeout=60000)
                if response and response.status >= 400:
                    raise RuntimeError("ResearchGate access is restricted")
                page.wait_for_timeout(1500)
                rows = collect_page(page)
                if not rows:
                    raise RuntimeError("No public publication metadata on this page")
                records.extend(rows)
            except Exception as exc:
                failed_pages += 1
                print(f"ResearchGate page was unavailable ({type(exc).__name__}); retaining other results.", flush=True)
            if len(urls) > 1:
                page.wait_for_timeout(3000)
        browser.close()
    if not records:
        raise SystemExit("No publication metadata collected. Check login and URLs; previous export was preserved.")
    records = normalize_records(records)
    save_export(records, Path(args.output), failed_pages)
    print(f"Exported {len(records)} ResearchGate publication records ({failed_pages} unavailable pages).", flush=True)


def _authors(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s*(?:;|\band\b|\n)\s*", value, flags=re.I)
            if part.strip()]


def _year(value: str) -> str:
    match = re.search(r"\b(?:19|20)\d{2}\b", value or "")
    return match.group(0) if match else ""


def _doi(value: str) -> str:
    match = re.search(r"(?:doi\.org/|\bdoi:\s*)(10\.\d{4,9}/[^\s<>\]\[\"']+)", value or "", re.I)
    return match.group(1).rstrip(".,;)") if match else ""


if __name__ == "__main__":
    main()
