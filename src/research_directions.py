"""Editable research interests shared by collection, selection and the website."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = 'config/research-directions.json'
MAX_DIRECTIONS = 12


def clean_profile(value):
    if not isinstance(value, dict) or value.get('version') != 1:
        raise ValueError('研究方向配置版本无效。')
    result = {'version': 1}
    for key in ('core_count', 'extended_count'):
        count = value.get(key)
        if type(count) is not int or not 1 <= count <= 20:
            raise ValueError('核心推荐和扩展阅读各设置 1–20 篇。')
        result[key] = count
    rows = value.get('directions')
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_DIRECTIONS:
        raise ValueError(f'可管理 1–{MAX_DIRECTIONS} 个研究方向。')
    cleaned, identifiers, names = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('研究方向条目无效。')
        identifier = str(row.get('id', ''))
        name = ' '.join(str(row.get('name', '')).split())
        if not re.fullmatch(r'[a-z][a-z0-9_-]{2,47}', identifier) or not 2 <= len(name) <= 60:
            raise ValueError('请填写 2–60 字的方向名称。')
        if identifier in identifiers or name.casefold() in names:
            raise ValueError('方向名称或编号重复，请编辑已有方向。')
        identifiers.add(identifier); names.add(name.casefold())
        item = {'id': identifier, 'name': name}
        for key in ('enabled', 'core', 'extended'):
            if type(row.get(key)) is not bool:
                raise ValueError('请选择方向的启用状态与推荐位置。')
            item[key] = row[key]
        if item['enabled'] and not (item['core'] or item['extended']):
            raise ValueError('启用的方向至少参与核心推荐或扩展阅读。')
        if type(row.get('weight')) is not int or row['weight'] not in (1, 2, 3):
            raise ValueError('方向优先级无效。')
        item['weight'] = row['weight']
        for key in ('keywords', 'require_any', 'exclude'):
            terms = row.get(key, [])
            if not isinstance(terms, list) or len(terms) > 24:
                raise ValueError('每组最多填写 24 个关键词。')
            values = []
            for term in terms:
                if not isinstance(term, str) or any(ord(c) < 32 for c in term):
                    raise ValueError('关键词必须是普通文字。')
                term = ' '.join(term.strip().split())
                if not 1 <= len(term) <= 80 or not any(c.isalnum() for c in term):
                    raise ValueError('每个关键词填写 1–80 字。')
                if term.casefold() not in {t.casefold() for t in values}:
                    values.append(term)
            if key == 'keywords' and not values:
                raise ValueError('每个方向至少填写一个主题关键词。')
            item[key] = values
        cleaned.append(item)
    if not any(d['enabled'] for d in cleaned):
        raise ValueError('至少启用一个研究方向。')
    result['directions'] = cleaned
    return result


def load_profile(root=ROOT):
    path = Path(root) / '.local/research-directions/applied.json'
    if not path.exists():
        path = Path(root) / CONFIG_PATH
    if not path.exists():
        path = ROOT / CONFIG_PATH
    return clean_profile(json.loads(path.read_text(encoding='utf-8')))


def profile_revision(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def active_directions(profile, tier=None):
    return [d for d in profile['directions'] if d['enabled'] and (tier is None or d[tier])]


def normalize(text):
    return re.sub(r'\s+', ' ', re.sub(r'[‐‑–—-]', ' ', str(text).casefold()))


def term_matches(term, text):
    term = normalize(term)
    pattern = re.escape(term)
    if term[0].isascii() and term[0].isalnum():
        pattern = r'(?<![a-z0-9])' + pattern
    if term[-1].isascii() and term[-1].isalnum():
        pattern += r's?(?![a-z0-9])'
    return bool(re.search(pattern, text))


def match_directions(record, profile):
    text = normalize(record.title + ' ' + (record.abstract or ''))
    return [d['id'] for d in active_directions(profile)
            if any(term_matches(t, text) for t in d['keywords'])
            and (not d['require_any'] or any(term_matches(t, text) for t in d['require_any']))
            and not any(term_matches(t, text) for t in d['exclude'])]


def select_balanced(papers, profile, tier, limit, excluded=()):
    """Weighted fair allocation; no duplicates or unrelated filler."""
    selected, used = [], set(excluded)
    directions = active_directions(profile, tier)
    counts = {d['id']: 0 for d in directions}
    while len(selected) < limit:
        choices = []
        for i, direction in enumerate(directions):
            candidates = [p for p in papers if p['id'] not in used and direction['id'] in p.get('topic_tags', [])]
            if candidates:
                choices.append((counts[direction['id']] / direction['weight'], -direction['weight'],
                                len(candidates), i, candidates[0]))
        if not choices:
            break
        _, _, _, i, paper = min(choices, key=lambda c: c[:4])
        identifier = directions[i]['id']
        selected.append({**paper, 'recommended_direction': identifier})
        used.add(paper['id']); counts[identifier] += 1
    return selected


def select_tiers(papers, profile):
    core = select_balanced(papers, profile, 'core', profile['core_count'])
    extended = select_balanced(papers, profile, 'extended', profile['extended_count'], (p['id'] for p in core))
    return core, extended


def query_plan(profile):
    """Keep every active direction, with bounded queries for quota-limited APIs.

    General search uses two alternative short queries per direction. Boolean
    indexes use quoted alternatives; the full keyword lists refine local matches.
    """
    plain, boolean, arxiv, semantic = [], [], [], []
    quote = lambda s: '"' + re.sub(r'["\\]', ' ', s) + '"'
    for d in active_directions(profile):
        terms, methods = d['keywords'][:2], d['require_any'][:2]
        plain.extend(' '.join([term, *methods[:1]]) for term in terms)
        primary = '(' + ' OR '.join(map(quote, terms)) + ')'
        method = '(' + ' OR '.join(map(quote, methods)) + ')'
        boolean.append(primary + (' AND ' + method if methods else ''))
        arxiv.append('(' + ' OR '.join('all:' + quote(t) for t in terms) + ')' +
                     (' AND (' + ' OR '.join('all:' + quote(t) for t in methods) + ')' if methods else ''))
        semantic.append('(' + ' | '.join(map(quote, terms)) + ')' +
                        (' + (' + ' | '.join(map(quote, methods)) + ')' if methods else ''))
    groups = [' OR '.join('(' + q + ')' for q in boolean[i::3]) for i in range(min(3, len(boolean)))]
    return {'plain': list(dict.fromkeys(plain)), 'boolean': boolean, 'bounded_boolean': groups,
            'arxiv': ' OR '.join('(' + q + ')' for q in arxiv),
            'semantic': ' | '.join('(' + q + ')' for q in semantic)}
