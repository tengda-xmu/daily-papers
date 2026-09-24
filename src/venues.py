"""Journal classification and CNS queries backed by the shared directory."""
from src.catalog import JOURNALS, match_journal

CNS_CORE_JOURNALS = tuple(item.get('canonical_name', item['name']) for item in JOURNALS if item['group'] == 'CNS 正刊')
CNS_SUBJOURNALS = tuple(item.get('canonical_name', item['name']) for item in JOURNALS if item['group'] == 'CNS 子刊')


def classify_venue(value: str) -> str:
    journal = match_journal(value)
    return journal['group'] if journal else ''


def venue_priority(value: str) -> float:
    return {'CNS 子刊': 3.0, 'CNS 正刊': 2.0}.get(classify_venue(value), 1.0 if classify_venue(value) else 0.0)


def cns_query() -> str:
    return '(' + ' OR '.join(f'SRCTITLE("{name}")' for name in CNS_CORE_JOURNALS + CNS_SUBJOURNALS) + ')'
