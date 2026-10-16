"""Journal classification and CNS queries backed by the shared directory."""
from src.catalog import JOURNALS, match_journal

CNS_CORE_JOURNALS = tuple(item.get('canonical_name', item['name']) for item in JOURNALS if item['group'] == 'CNS 正刊')
CNS_SUBJOURNALS = tuple(item.get('canonical_name', item['name']) for item in JOURNALS if item['group'] == 'CNS 子刊')


def classify_venue(value: str) -> str:
    journal = match_journal(value)
    return journal['group'] if journal else ''


def venue_priority(value: str) -> float:
    return {'CNS 子刊': 3.0, 'CNS 正刊': 3.0}.get(classify_venue(value), 1.0 if classify_venue(value) else 0.0)


def cns_journals() -> list[dict]:
    """Discovery and classification share one explicit journal directory."""
    return [dict(item) for item in JOURNALS if item['group'] in ('CNS 正刊', 'CNS 子刊') and item.get('issn')]


def journal_issns(journal: dict) -> tuple[str, ...]:
    return tuple(dict.fromkeys([journal['issn'], *journal.get('issns', [])]))


def cns_query() -> str:
    return '(' + ' OR '.join(f'SRCTITLE("{name}")' for name in CNS_CORE_JOURNALS + CNS_SUBJOURNALS) + ')'
