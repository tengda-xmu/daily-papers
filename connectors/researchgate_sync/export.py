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
from pathlib import Path
from urllib.parse import urljoin


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", action="append", dest="urls", default=[])
    parser.add_argument("--output", default="data/inbox/researchgate.json")
    parser.add_argument("--session", default=os.getenv("RESEARCHGATE_SESSION_DIR", ".local/researchgate"))
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
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
        for target in urls:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
            for item in page.locator("article, [data-testid*='publication'], .nova-legacy-o-stack").all():
                title_node = item.locator("a[href*='/publication/'], h3, h2").first
                if not title_node.count():
                    continue
                title = title_node.inner_text().strip()
                href = urljoin(page.url, title_node.get_attribute("href") or page.url)
                records.append({"source": "researchgate", "title": title, "landing_url": href})
        browser.close()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
