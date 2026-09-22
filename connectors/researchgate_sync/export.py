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
from pathlib import Path
from urllib.parse import urljoin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", action="append", dest="urls", default=[])
    parser.add_argument("--output", default="data/inbox/researchgate.json")
    parser.add_argument("--session", default=os.getenv("RESEARCHGATE_SESSION_DIR", ".local/researchgate"))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--login", action="store_true", help="Wait for manual login before collecting")
    args = parser.parse_args()
    if args.login and args.headless:
        parser.error("--login requires a visible browser; omit --headless")
    urls = args.urls or [x.strip() for x in os.getenv("RESEARCHGATE_AUTHOR_URLS", "").split(",") if x.strip()]
    if not urls:
        raise SystemExit("Provide --url or RESEARCHGATE_AUTHOR_URLS")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Install connector dependencies with: pip install -r connectors/researchgate_sync/requirements.txt") from exc
    records = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(args.session, headless=args.headless)
        page = browser.pages[0] if browser.pages else browser.new_page()
        if args.login:
            page.goto("https://www.researchgate.net/login", wait_until="domcontentloaded", timeout=60000)
            input("Complete ResearchGate login in the browser, then press Enter here to collect metadata: ")
        seen = set()
        for target in urls:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
            for item in page.locator("article, [data-testid*='publication'], .nova-legacy-o-stack").all():
                title_node = item.locator("a[href*='/publication/'], h3, h2").first
                if not title_node.count():
                    continue
                title = title_node.inner_text().strip()
                href = urljoin(page.url, title_node.get_attribute("href") or page.url)
                key = (title.casefold(), href)
                if key in seen:
                    continue
                seen.add(key)
                context = item.inner_text()
                authors = _authors(_text(item, ".author, [class*='author'], [data-testid*='author']"))
                year = _year(_text(item, "time, [class*='year'], [data-testid*='year']") or context)
                venue = _text(item, ".journal, .publication, [class*='journal'], [class*='venue']")
                doi = _doi(context)
                records.append({
                    "source": "ResearchGate", "title": title, "authors": authors,
                    "published_at": year, "venue": venue, "doi": doi,
                    "landing_url": href, "researchgate_url": href,
                })
        browser.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if not records:
        raise SystemExit("No publication metadata collected. Check login and URLs; previous export was preserved.")
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _text(item, selector: str) -> str:
    try:
        node = item.locator(selector).first
        return node.inner_text().strip() if node.count() else ""
    except Exception:
        return ""


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
