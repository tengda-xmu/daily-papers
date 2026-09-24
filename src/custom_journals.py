"""Validated journal subscriptions, shared by the directory and daily search."""
from copy import deepcopy
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATH = 'config/custom-journals.json'
LOCAL_PATH = '.local/journals/state.json'
MAX_JOURNALS = 25


def normalize_issn(value):
    text = str(value).strip().upper().replace('-', '')
    if not re.fullmatch(r'[0-9]{7}[0-9X]', text):
        raise ValueError('ISSN 格式应为 1234-567X，共 8 位。')
    digits = [int(c) if c != 'X' else 10 for c in text]
    if sum(n * weight for n, weight in zip(digits, range(8, 0, -1))) % 11:
        raise ValueError('ISSN 校验位不正确，请核对纸质版或电子版 ISSN。')
    return text[:4] + '-' + text[4:]


def name_key(value):
    return re.sub(r'\s+', ' ', value.strip().casefold().replace('&', ' and '))


def clean_journals(rows):
    if not isinstance(rows, list) or len(rows) > MAX_JOURNALS:
        raise ValueError(f'最多管理 {MAX_JOURNALS} 本定向检索期刊。')
    result, identifiers, names = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('期刊条目格式无效。')
        name = ' '.join(str(row.get('name', '')).split())
        canonical = ' '.join(str(row.get('canonical_name') or name).split())
        group = ' '.join(str(row.get('group', '自定义期刊')).split())
        if not 2 <= len(name) <= 180 or not 2 <= len(canonical) <= 180 or not 1 <= len(group) <= 60:
            raise ValueError('请填写有效的期刊名称和分组。')
        issn = normalize_issn(row.get('issn', ''))
        aliases = sorted({issn, *(normalize_issn(x) for x in row.get('issns', []))})
        if len(aliases) > 6 or identifiers.intersection(aliases) or names.intersection({name_key(name), name_key(canonical)}):
            raise ValueError('该期刊已添加（名称或纸质版/电子版 ISSN 重复）。')
        if type(row.get('enabled', True)) is not bool:
            raise ValueError('期刊启用状态无效。')
        identifiers.update(aliases); names.update({name_key(name), name_key(canonical)})
        result.append({'issn': issn, 'issns': aliases, 'name': name, 'canonical_name': canonical, 'group': group,
                       'enabled': row.get('enabled', True)})
    return sorted(result, key=lambda r: r['issn'])


def published_journals(root=ROOT):
    path = Path(root) / PUBLIC_PATH
    return clean_journals(json.loads(path.read_text(encoding='utf-8')).get('journals', [])) if path.exists() else []


def load_custom_journals(root=ROOT):
    path = Path(root) / LOCAL_PATH
    if path.exists():
        return clean_journals(json.loads(path.read_text(encoding='utf-8'))['journals'])
    return published_journals(root)


def journal_groups(root=ROOT):
    path = Path(root) / 'config/venues.yml'
    groups = deepcopy((yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('groups', [])) if path.exists() else []
    for journal in load_custom_journals(root):
        # Enabling an existing catalog journal must not duplicate it.
        platform = 'Crossref'
        for group in groups:
            for value in group['journals'][:]:
                item = {'name': value} if isinstance(value, str) else value
                if name_key(item['name']) in {name_key(journal['name']), name_key(journal['canonical_name'])}:
                    platform = item.get('platform') or group.get('platform') or platform
                    group['journals'].remove(value)
        group = next((g for g in groups if g['id'] == journal['group']), None)
        if group is None:
            group = {'id': journal['group'], 'journals': []}
            groups.append(group)
        group['journals'].append({**journal, 'platform': platform, 'custom': True})
    return groups
