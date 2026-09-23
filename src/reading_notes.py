"""Validated Chinese reading notes, with explicit evidence and provenance."""
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from src.models import RawRecord, in_date_window

ROOT = Path(__file__).resolve().parents[1]
CURATED = ROOT / "data/curated/reading-notes.json"
CACHE = ROOT / "data/analyses"
NOTE_FIELDS = ("problem", "method", "innovation", "findings", "limitations", "connection", "next_steps")


def chinese_count(text):
    return len(re.findall(r"[\u4e00-\u9fff]", str(text or "")))


def valid_analysis(value):
    if not isinstance(value, dict) or value.get("analysis_status") != "ready":
        return False
    notes = value.get("deep_read") or {}
    sources = value.get("analysis_sources")
    if not isinstance(notes, dict) or not isinstance(sources, list) or not sources:
        return False
    try:
        if not all(isinstance(url, str) and urlsplit(url).scheme in ("https", "http")
                   and urlsplit(url).netloc for url in sources):
            return False
    except ValueError:
        return False
    return (chinese_count(value.get("summary")) >= 40
            and chinese_count(value.get("title_zh")) >= 6
            and chinese_count(value.get("recommendation")) >= 12
            and all(isinstance(notes.get(key), str) and chinese_count(notes[key]) >= 35 for key in NOTE_FIELDS)
            and len({notes[key] for key in NOTE_FIELDS}) == len(NOTE_FIELDS))


def curated_entries():
    try:
        rows = json.loads(CURATED.read_text(encoding="utf-8"))["entries"]
        return [row for row in rows if valid_analysis(row.get("analysis"))]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def curated_records(until, lookback_days=180):
    start = until - timedelta(days=lookback_days)
    return [RawRecord.from_mapping(row["paper"]) for row in curated_entries()
            if in_date_window(row["paper"].get("published_at"), start, until)]


def _cache_path(record):
    # Recompute automatic notes when title or available abstract changes.
    value = "zh-v2|" + record.doi + "|" + record.title + "|" + record.abstract
    return CACHE / (hashlib.sha256(value.encode()).hexdigest()[:24] + ".json")


def cached_analysis(record):
    for row in curated_entries():
        if record.doi and row["paper"].get("doi", "").lower() == record.doi:
            return row["analysis"]
    try:
        result = json.loads(_cache_path(record).read_text(encoding="utf-8"))
        return result if valid_analysis(result) else None
    except (OSError, ValueError):
        return None


def save_analysis(record, result):
    if not valid_analysis(result):
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    _cache_path(record).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
