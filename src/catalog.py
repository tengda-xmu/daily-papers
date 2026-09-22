"""Shared, configuration-backed source, journal and topic filter directory."""
from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml

CONFIG = Path(__file__).resolve().parents[1] / 'config'


def read_config(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text(encoding='utf-8')) or {}


SOURCE_CATALOG = read_config('sources.yml')['sources']
VENUE_GROUPS = read_config('venues.yml')['groups']
TOPIC_CATALOG = read_config('topics.yml')['topics']
JOURNALS = []
for group in VENUE_GROUPS:
    for value in group['journals']:
        journal = {'name': value} if isinstance(value, str) else dict(value)
        JOURNALS.append({'platform': group.get('platform', ''), **journal, 'group': group['id']})


def normalized(value: str) -> str:
    text = html.unescape(str(value or '')).casefold()
    return re.sub(r'\s+', ' ', text.replace('&', ' and ')).strip()


def match_journal(value: str) -> dict | None:
    venue = normalized(value)
    # Longer names win (Cell Reports Physical Science before Cell Reports).
    for item in sorted(JOURNALS, key=lambda entry: len(entry['name']), reverse=True):
        title = normalized(item['name'])
        if item['group'] == 'CNS 正刊':
            # Scholar summaries contain author/year/venue segments. A prefix
            # such as "Science of the Total Environment" is not Science.
            segments = re.split(r'\s+[-–—]\s+', venue)
            if any(part == title or part in (title + ' (london)', title + ' (new york, n.y.)')
                   for part in segments):
                return item
        elif re.search(r'(?<!\w)' + re.escape(title) + r'(?!\w)', venue):
            return item
    return None


def string_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(item) for item in (value or []) if item]


def canonical_source(value: str) -> str:
    for item in SOURCE_CATALOG:
        if normalized(value) in {normalized(alias) for alias in
                                 [item['id'], item['label'], *item.get('aliases', [])]}:
            return item['id']
    return str(value or '')


def paper_facets(paper: dict) -> dict:
    metadata = paper.get('raw_metadata') or {}
    sources = set(canonical_source(item) for item in
                  [paper.get('source', ''), *string_list(metadata.get('sources')),
                   *string_list(paper.get('sources'))] if item)
    journal = match_journal(paper.get('venue', ''))
    if journal:
        sources.add(journal['platform'])
    try:
        host = (urlsplit(paper.get('landing_url', '')).hostname or '').lower()
    except ValueError:
        host = ''
    for item in SOURCE_CATALOG:
        if any(host == domain or host.endswith('.' + domain) for domain in item.get('domains', [])):
            sources.add(item['id'])
    if host == 'arxiv.org' or host.endswith('.arxiv.org'):
        sources.add('arXiv')

    if journal:
        group = journal['group']
    elif '微信公众号' in sources:
        group = '公众号文章'
    elif 'arXiv' in sources:
        group = '预印本'
    else:
        group = next((item['id'] for item in VENUE_GROUPS
                      if item.get('platform') in sources), '其他／未分类')
    return {'source_ids': sorted(sources - {''}), 'venue_group': group,
            'journal': journal['name'] if journal else str(paper.get('venue') or ''),
            'topic_ids': string_list(paper.get('topic_tags'))}


STATE_LABELS = {
    'ok': '本轮正常', 'configuration_missing': '配置缺失', 'not_run': '本轮未运行',
    'no_data': '本轮无数据', 'error': '抓取失败', 'access_denied': '访问受限',
    'quota_exhausted': '配额已用尽', 'authorization_required': '需要机构授权',
    'planned': '待接入', 'platform': '期刊平台筛选',
}


def source_state(item: dict, statuses: dict) -> tuple[str, str]:
    if item['kind'] == 'platform':
        return 'platform', '按期刊名称或原文链接筛选已有记录；未独立抓取此平台。'
    if item['kind'] == 'planned':
        return 'planned', '已列入来源目录，自动抓取尚未接入。'
    status = statuses.get(item['id']) or {}
    return status.get('status', 'not_run'), status.get('message', '')
