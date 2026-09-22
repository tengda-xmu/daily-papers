"""Journal classification and CNS queries backed by the shared directory."""
from src.catalog import JOURNALS, match_journal

CNS_CORE_JOURNALS = tuple(item['name'] for item in JOURNALS if item['group'] == 'CNS 正刊')
CNS_SUBJOURNALS = tuple(item['name'] for item in JOURNALS if item['group'] == 'CNS 子刊')


def classify_venue(value: str) -> str:
    journal = match_journal(value)
    return journal['group'] if journal else ''


def venue_priority(value: str) -> float:
    return {'CNS 正刊': 1.0, 'CNS 子刊': 0.8}.get(classify_venue(value), 0.0)


def cns_query() -> str:
    return '(' + ' OR '.join(f'SRCTITLE("{name}")' for name in CNS_CORE_JOURNALS + CNS_SUBJOURNALS) + ')'
