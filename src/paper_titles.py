"""Chinese paper titles are metadata, independent of full reading notes."""
import json
import re
from pathlib import Path

from src.models import normalize_doi

DATA = Path(__file__).resolve().parents[1] / 'data'


def title_key(value):
    return ' '.join(str(value or '').split()).casefold()


def valid_title(value):
    return isinstance(value, str) and 2 <= len(re.findall(r'[\u4e00-\u9fff]', value)) and len(value) <= 600


def title_entries(data=DATA):
    entries = {}
    # Reviewed translations take precedence over machine translations.
    for path in (Path(data) / 'paper-titles.json', Path(data) / 'curated/paper-titles.json'):
        try:
            rows = json.loads(path.read_text(encoding='utf-8')).get('entries', [])
        except (OSError, ValueError):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get('title') and valid_title(row.get('title_zh')):
                entries[title_key(row['title'])] = row
    return entries


def chinese_title(paper, entries=None):
    existing = paper.get('title_zh')
    if valid_title(existing):
        return existing.strip()
    title = paper.get('title') or ''
    if valid_title(title):
        return title
    row = (title_entries() if entries is None else entries).get(title_key(title))
    if not row:
        return ''
    doi, recorded_doi = normalize_doi(paper.get('doi')), normalize_doi(row.get('doi'))
    if doi and recorded_doi and doi != recorded_doi:
        return ''
    return row['title_zh'].strip()
