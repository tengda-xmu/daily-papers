"""Verified publisher figures for DOI-matched reading recommendations."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from src.models import normalize_doi

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/curated/figures.json"


@lru_cache(maxsize=1)
def figure_catalog() -> dict:
    try:
        entries = json.loads(CATALOG.read_text(encoding="utf-8"))["entries"]
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def get_figure(doi: str) -> dict | None:
    figure = figure_catalog().get(normalize_doi(doi))
    if not isinstance(figure, dict):
        return None
    path = figure.get("image_path", "")
    if not isinstance(path, str) or not re.fullmatch(r"assets/figures/[a-z0-9-]+\.png", path):
        return None
    if not (ROOT / "tools" / path).is_file():
        return None
    for key in ("source_url", "license_url"):
        try:
            parsed = urlsplit(str(figure.get(key, "")))
            if parsed.scheme != "https" or not parsed.netloc:
                return None
        except ValueError:
            return None
    if not all(isinstance(figure.get(key), str) and figure[key].strip()
               for key in ("figure_label", "title_zh", "caption_zh", "credit", "license")):
        return None
    if not all(type(figure.get(key)) is int and figure[key] > 0 for key in ("width", "height")):
        return None
    return dict(figure)
