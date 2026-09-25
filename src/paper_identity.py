"""Verified DOI aliases without changing published recommendation identities."""
import json
from pathlib import Path
from src.models import normalize_doi

CATALOG = Path(__file__).resolve().parents[1] / 'data/curated/paper-identities.json'


def paper_doi(paper):
    doi = normalize_doi(paper.get('doi'))
    if doi:
        return doi
    try:
        rows = json.loads(CATALOG.read_text(encoding='utf-8'))['entries']
    except (OSError, ValueError, KeyError):
        return ''
    title = ' '.join(str(paper.get('title', '')).casefold().split())
    for row in rows:
        if paper.get('id') in row['paper_ids'] and title == row['title'].casefold():
            return normalize_doi(row['doi'])
    return ''
