"""Verified publisher figures for DOI-matched reading recommendations."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from src.models import normalize_doi

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/curated/figures.json"


def read_catalog(path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def figure_catalog() -> dict:
    automatic = read_catalog(ROOT / 'data/figures/catalog.json').get('entries', {})
    curated = read_catalog(CATALOG).get('entries', {})
    return {**(automatic if isinstance(automatic, dict) else {}),
            **(curated if isinstance(curated, dict) else {})}


def figure_status(doi: str) -> str:
    checks = read_catalog(ROOT / 'data/figures/catalog.json').get('checks', {})
    return checks.get(normalize_doi(doi), {}).get('state', '')


def figure_file(figure: dict) -> Path | None:
    path = figure.get('image_path', '')
    if not isinstance(path, str) or not re.fullmatch(r'assets/figures/[a-z0-9-]+\.(?:png|jpg|jpeg)', path):
        return None
    for candidate in (ROOT / 'tools' / path, ROOT / 'data/figures/images' / Path(path).name):
        if candidate.is_file():
            return candidate
    return None


def get_figure(doi: str) -> dict | None:
    figure = figure_catalog().get(normalize_doi(doi))
    if not isinstance(figure, dict):
        return None
    if not figure_file(figure):
        return None
    for key in ("source_url", "license_url"):
        try:
            parsed = urlsplit(str(figure.get(key, "")))
            if parsed.scheme != "https" or not parsed.netloc:
                return None
        except ValueError:
            return None
    if not all(isinstance(figure.get(key), str) and figure[key].strip()
               for key in ("figure_label", "credit", "license")):
        return None
    for translated, original in (("title_zh", "title"), ("caption_zh", "caption")):
        text = figure.get(translated) or figure.get(original)
        if not isinstance(text, str) or not text.strip():
            return None
    if not all(type(figure.get(key)) is int and figure[key] > 0 for key in ("width", "height")):
        return None
    return dict(figure)
