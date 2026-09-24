"""Explicit search ordering and deterministic ranking of the returned candidates."""
from __future__ import annotations

import re
from src.models import parse_date

SORT_OPTIONS = [
    {'id': 'relevance', 'label': '相关性优先'},
    {'id': 'latest', 'label': '最新发表'},
    {'id': 'citations', 'label': '热度（被引次数）'},
]
SORT_LABELS = {s['id']: s['label'] for s in SORT_OPTIONS}


def sort_note(source, order):
    label = SORT_LABELS[order]
    if source in ('CNS 子刊专项', 'Crossref', 'Elsevier', 'OpenAlex', 'Semantic Scholar'):
        return f'来源按{label}检索；合并后在本次返回结果中排序。'
    if source in ('arXiv', 'PubMed'):
        return ('该接口未提供被引次数，热度不可比较，结果排在有被引数据的记录之后。'
                if order == 'citations' else f'来源按{label}检索。')
    if source in ('Google Scholar', 'ResearchGate'):
        return ('按来源相关性取得候选，再在候选内按' + label + '排序。'
                '不会将 Scholar 的“过去一年新增”当作全部年份的最新发表。'
                if order == 'latest' else '按来源相关性取得候选，再在候选内按被引次数排序。'
                if order == 'citations' else '使用来源相关性与关键词匹配合并排序。')
    if source == '微信公众号':
        return ('公众号未提供可比的论文被引次数；不以阅读量冒充被引热度。'
                if order == 'citations' else f'订阅文章与公开索引候选按{label}重排，不代表全库排名。')
    return f'当前接入取回候选后按{label}重排，不代表全库排名；被引数据以账号授权为准。'


def date_value(value):
    parsed = parse_date(value)
    return parsed.timestamp() if parsed else float('-inf')


def citation_value(value):
    return value if type(value) is int and value >= 0 else -1


def order_candidates(rows, order):
    # Stable ties preserve the source's own relevance order. Missing counts
    # remain distinct from a reported count of zero.
    if order == 'latest':
        return sorted(rows, key=lambda r: date_value(r.published_at), reverse=True)
    if order == 'citations':
        return sorted(rows, key=lambda r: citation_value(r.citation_count), reverse=True)
    return rows


def rank_results(records, query, order):
    """Fuse source ranks for relevance; retain citation counts with provenance.

    Counts from different indexes are never summed. The largest reported
    count is used as an explicit, inspectable proxy for citation popularity.
    """
    terms = re.findall(r'[\w.-]+', query.casefold())
    for paper in records:
        title = paper['title'].casefold()
        text = title + ' ' + (paper.get('abstract') or '').casefold()
        evidence = paper.pop('_search_observations', [])
        ranks = [r['rank'] for r in evidence]
        fusion = sum(1 / (60 + rank) for rank in ranks) if order == 'relevance' else 0
        coverage = sum((2 if t in title else 1 if t in text else 0) for t in terms) / max(1, 2 * len(terms))
        paper['relevance_score'] = round(fusion + .02 * coverage, 8)
        counts = [dict(source=e['source'], count=e['citation_count']) for e in evidence
                  if citation_value(e.get('citation_count')) >= 0]
        counts.sort(key=lambda c: (-c['count'], c['source']))
        paper['citation_counts'] = counts
        paper['citation_count'] = counts[0]['count'] if counts else None
        paper['ranks'] = {}
    key = lambda p: (-p['relevance_score'], p['id'])
    comparators = {
        'relevance': key,
        'latest': lambda p: (-date_value(p.get('published_at')), *key(p)),
        'citations': lambda p: (-citation_value(p.get('citation_count')), *key(p)),
    }
    for mode, compare in comparators.items():
        for rank, paper in enumerate(sorted(records, key=compare)):
            paper['ranks'][mode] = rank
    return sorted(records, key=lambda p: p['ranks'][order])
