"""Manual subscriptions shared by local collection and the published directory."""
import json
from pathlib import Path
import re
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_PATH = 'config/custom-wechat.json'
LOCAL_PATH = '.local/wechat-subscriptions/state.json'
MAX_ACCOUNTS = 100


def name_key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def clean_accounts(rows):
    if not isinstance(rows, list) or len(rows) > MAX_ACCOUNTS:
        raise ValueError(f'最多手动管理 {MAX_ACCOUNTS} 个公众号。')
    result, ids, names, aliases = [], set(), set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('公众号条目格式无效。')
        identifier = row.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier) or identifier in ids:
            raise ValueError('公众号编号无效或重复。')
        fields = {}
        for field, default, maximum in (('name', '', 100), ('alias', '', 80), ('group', '科研综合', 60)):
            value = row.get(field, default)
            if not isinstance(value, str) or re.search(r'[<>\x00-\x1f\x7f]', value):
                raise ValueError('请填写有效的公众号名称、微信号和分组。')
            value = ' '.join(value.split())
            if len(value) > maximum or (field != 'alias' and not value):
                raise ValueError('请填写有效的公众号名称和分组。')
            fields[field] = value
        if '://' in fields['name'] or (fields['alias'] and not re.fullmatch(r'[A-Za-z0-9_-]+', fields['alias'])):
            raise ValueError('请填写公众号名称；微信号只支持字母、数字、下划线或短横线，不要填写链接。')
        key, alias = name_key(fields['name']), name_key(fields['alias'])
        if key in names or (alias and alias in aliases):
            raise ValueError('这个公众号已添加（名称或微信号重复）。')
        enabled = row.get('enabled', True)
        if type(enabled) is not bool:
            raise ValueError('订阅启用状态无效。')
        ids.add(identifier); names.add(key)
        if alias:
            aliases.add(alias)
        result.append({'id': identifier, **fields, 'enabled': enabled})
    return sorted(result, key=lambda row: row['id'])


def published_accounts(root=ROOT):
    path = Path(root) / PUBLIC_PATH
    return clean_accounts(json.loads(path.read_text(encoding='utf-8'))['accounts']) if path.exists() else []


def load_manual_accounts(root=ROOT):
    path = Path(root) / LOCAL_PATH
    return clean_accounts(json.loads(path.read_text(encoding='utf-8'))['accounts']) if path.exists() else published_accounts(root)


def effective_accounts(root=ROOT, directory=None, *, manual=None):
    """Merge without modifying WeRSS snapshots; manual entries take precedence."""
    root = Path(root)
    path = Path(directory) if directory is not None else root / 'data/inbox/wechat-subscriptions.json'
    snapshot = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    rows = snapshot.get('accounts', [])
    if not rows:
        policy = root / 'config/wechat_accounts.json'
        seeds = json.loads(policy.read_text(encoding='utf-8')).get('seed_names', []) if policy.exists() else []
        rows = [{'name': name} for name in seeds]
    merged = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get('name'), str) and row['name'].strip():
            merged[name_key(row['name'])] = {'name': row['name'], 'alias': row.get('alias', ''),
                'groups': row.get('groups') or ['科研综合'], 'manual': False, 'enabled': True}
    for row in load_manual_accounts(root) if manual is None else manual:
        merged[name_key(row['name'])] = {**row, 'groups': [row['group']], 'manual': True}
    return list(merged.values())


def group_overview(root=ROOT, *, manual=None):
    """Count unique accounts per group, retaining overlapping group memberships."""
    policy = Path(root) / 'config/wechat_accounts.json'
    configured = json.loads(policy.read_text(encoding='utf-8')).get('groups', []) if policy.exists() else []
    groups = {}
    for row in [*configured, {'name': '科研综合', 'description': '跨学科科研资讯、学术资源及暂未细分的研究方向'}]:
        key = name_key(row['name'])
        groups.setdefault(key, {'name': row['name'], 'description': row.get('description', ''),
            'total': 0, 'enabled': 0, 'custom': False})
    accounts = effective_accounts(root, manual=manual)
    multiple = 0
    for account in accounts:
        memberships = {name_key(group): group for group in account['groups']}
        multiple += len(memberships) > 1
        for key, name in memberships.items():
            group = groups.setdefault(key, {'name': name, 'description': '自定义研究方向',
                'total': 0, 'enabled': 0, 'custom': True})
            group['total'] += 1
            group['enabled'] += int(account['enabled'])
    return {'groups': list(groups.values()), 'total': len(accounts),
        'enabled': sum(row['enabled'] for row in accounts), 'multi_group': multiple}


def manual_queries(accounts, until):
    """Rotate up to two names per day, reserving capacity for topic queries."""
    names = sorted({row['name'] for row in accounts if row.get('manual') and row['enabled']}, key=name_key)
    if not names:
        return []
    offset = until.date().toordinal() * min(2, len(names)) % len(names)
    return [f'{names[(offset + i) % len(names)]} {until.year}年{until.month}月' for i in range(min(2, len(names)))]
