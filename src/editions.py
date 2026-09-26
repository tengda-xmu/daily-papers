"""Published recommendation editions and identity history (no personal data)."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.models import RawRecord, parse_date

BEIJING = timezone(timedelta(hours=8))
ID = re.compile(r'[a-zA-Z0-9_-]{1,80}\Z')


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix=path.name + '.', suffix='.tmp', delete=False) as output:
        temp = Path(output.name)
        json.dump(value, output, ensure_ascii=False, indent=2)
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def edition_id(payload):
    identifier = str((payload.get('edition') or {}).get('id') or payload.get('update_run_id') or '')
    if ID.fullmatch(identifier):
        return identifier
    value = [payload.get('generated_at'), [[p.get('id') for p in payload.get(tier, [])] for tier in ('core', 'extended')]]
    return 'legacy-' + hashlib.sha256(json.dumps(value).encode()).hexdigest()[:20]


def entries(data):
    return manifest(data)['editions']


def manifest(data):
    return normalize_manifest(read(Path(data) / 'editions/index.json', {}))


def normalize_manifest(value):
    removed = {e['id'] for e in value.get('deleted', [])}
    return {**value, 'schema': 2, 'editions': [e for e in value.get('editions', []) if e['id'] not in removed],
            'deleted': value.get('deleted', []), 'operations': value.get('operations', {})}


def current_id(value):
    rows = value.get('editions', [])
    return max(rows, key=lambda e: (e['date'], e['number']))['id'] if rows else ''


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def candidates(data):
    return read(Path(data) / 'recommendation-candidates.json', {}).get('papers', {})


def remember_candidates(data, papers):
    from src.auto_reading import public_paper
    saved = candidates(data)
    for paper in papers:
        identifier = paper.get('id')
        if not re.fullmatch(r'[a-f0-9]{12}', str(identifier or '')):
            continue
        value = public_paper(paper)
        value.update({k: paper[k] for k in ('source', 'source_id', 'oa_url', 'pdf_url', 'citation_count', 'source_score') if paper.get(k) is not None})
        keys = aliases(paper) | (aliases(saved[identifier]) if identifier in saved else set())
        value['raw_metadata'] = {'identity_aliases': [list(k) for k in sorted(keys)]}
        saved[identifier] = value
    write(Path(data) / 'recommendation-candidates.json', {'schema': 1, 'papers': saved})


def prune_deleted(data):
    """Remove obsolete batch snapshots/aliases, preserving independent paper data."""
    data = Path(data)
    value = manifest(data)
    for row in value['deleted']:
        (data / relative_path(row)).unlink(missing_ok=True)
    for day in {e['date'] for e in value['deleted']}:
        rows = [e for e in value['editions'] if e['date'] == day]
        target = data / 'archive' / (day + '.json')
        if rows:
            latest = max(rows, key=lambda e: e['number'])
            payload = read(data / relative_path(latest))
            if payload:
                write(target, payload)
        else:
            target.unlink(missing_ok=True)


def delete_editions(data, identifiers, expected_revision, request_id):
    """All-or-nothing validation; journal first, then recoverable file cleanup."""
    data = Path(data)
    value = manifest(data)
    identifiers = sorted(set(identifiers))
    old = value['operations'].get(request_id)
    if old:
        if old['edition_ids'] != identifiers:
            raise ValueError('请求编号已用于另一组批次。')
        prune_deleted(data)
        return value
    if revision(value) != expected_revision:
        raise ValueError('批次列表已变化，请刷新后重新选择。')
    by_id = {e['id']: e for e in value['editions']}
    if not identifiers or any(i not in by_id for i in identifiers):
        raise ValueError('所选批次不存在，请刷新后重新选择。')
    protected = {current_id(value), read(data / 'daily.json').get('edition', {}).get('id')}
    if protected.intersection(identifiers):
        raise ValueError('当前推荐批次不能删除。')
    papers = []
    for identifier in identifiers:
        payload = read(data / relative_path(by_id[identifier]))
        if payload.get('edition') != by_id[identifier] or not selected(payload):
            raise ValueError('批次资料不完整，未执行删除。')
        papers.extend(selected(payload))
    remember_candidates(data, papers)
    when = datetime.now(timezone.utc).isoformat()
    value['deleted'].extend({**by_id[i], 'deleted_at': when, 'request_id': request_id} for i in identifiers)
    value['editions'] = [e for e in value['editions'] if e['id'] not in identifiers]
    value['operations'][request_id] = {'edition_ids': identifiers, 'deleted_at': when}
    write(data / 'editions/index.json', value)
    prune_deleted(data)
    return value


def valid_entry(row):
    return (isinstance(row, dict) and bool(ID.fullmatch(str(row.get('id', ''))))
            and bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(row.get('date', '')))))


def relative_path(row):
    if not valid_entry(row):
        raise ValueError('Invalid edition identity')
    return f"editions/{row['date']}/{row['id']}.json"


def archive_name(row):
    relative_path(row)
    return f"{row['date']}--{row['id']}"


def selected(payload):
    return (payload.get('core') or []) + (payload.get('extended') or [])


def append(data, payload, *, trigger='manual', day=None):
    """Stage a fixed edition. It becomes history only when these files publish."""
    data = Path(data)
    index = entries(data)
    state = manifest(data)
    identifier = edition_id(payload)
    if identifier in {e['id'] for e in state['deleted']}:
        return None
    existing = next((e for e in index if e['id'] == identifier), None)
    if existing:
        return read(data / relative_path(existing))
    if not selected(payload):
        return None
    when = parse_date(payload.get('generated_at'))
    if not when:
        raise ValueError('Edition requires a real generation time')
    day = day or when.astimezone(BEIJING).date().isoformat()
    row = {'id': identifier, 'date': day, 'number': 1 + max((e['number'] for e in index + state['deleted'] if e['date'] == day), default=0),
           'generated_at': payload['generated_at'], 'trigger': trigger,
           'core_count': len(payload.get('core', [])), 'extended_count': len(payload.get('extended', []))}
    snapshot = copy.deepcopy(payload)
    snapshot['edition'] = row
    # Candidate pools are not recommendations and must never consume eligibility.
    snapshot['papers'] = selected(snapshot)
    write(data / relative_path(row), snapshot)
    index.append(row)
    index.sort(key=lambda e: (e['date'], e['number']))
    write(data / 'editions/index.json', {**state, 'editions': index})
    write(data / 'archive' / (day + '.json'), snapshot)
    return snapshot


def aliases(paper):
    from src.pipeline import _identities
    keys = _identities(RawRecord.from_mapping(paper))
    for pair in (paper.get('raw_metadata') or {}).get('identity_aliases', []):
        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair):
            keys.add(tuple(pair))
    if paper.get('id'):
        keys.add(('id', paper['id']))
    return keys


class History:
    def __init__(self, data):
        self.by_alias, self.papers = {}, {}
        self.candidates = candidates(data)
        self.identities = {key: identifier for identifier, p in self.candidates.items() for key in aliases(p)}
        for entry in entries(data):
            payload = read(Path(data) / relative_path(entry))
            for tier in ('core', 'extended'):
                for paper in payload.get(tier, []):
                    keys = aliases(paper)
                    old = next((self.by_alias[k] for k in keys if k in self.by_alias), None)
                    identifier = old or paper['id']
                    state = self.papers.setdefault(identifier, {'id': identifier, 'paper': paper, 'appearances': []})
                    state['paper'] = {**paper, 'id': identifier}
                    appearance = {**entry, 'tier': tier}
                    state['appearances'].append(appearance)
                    state['latest'] = appearance
                    if tier == 'core':
                        state['last_core'] = appearance
                        evidence = (paper.get('recommendation_decision') or {}).get('heat') or {}
                        state.setdefault('used_attention', set()).update(e.get('story_id') for e in evidence.get('events', []) if e.get('story_id'))
                    for key in keys:
                        self.by_alias[key] = identifier
                        self.identities[key] = identifier

    def identity(self, paper):
        return next((self.identities[k] for k in sorted(aliases(paper)) if k in self.identities), paper.get('id'))

    def find(self, paper):
        identifier = next((self.by_alias[k] for k in aliases(paper) if k in self.by_alias), None)
        return self.papers.get(identifier)


def enrich(payload, analyses):
    """Notes may evolve; edition membership, order and decision evidence do not."""
    result = copy.deepcopy(payload)
    from src.auto_reading import public_analysis
    from src.paper_titles import chinese_title, title_entries
    from src.auto_reading import public_status
    from src.paper_titles import DATA
    titles = title_entries()
    for tier in ('core', 'extended', 'papers'):
        for paper in result.get(tier, []):
            item = analyses.get(paper.get('id'))
            if item:
                try:
                    from src.auto_reading import fingerprint
                    if item.get('fingerprint') == fingerprint(paper):
                        paper.update(public_analysis(item)['analysis'])
                        if item.get('material'):
                            paper['analysis_references'] = item['material']['references']
                            paper['analysis_material_version'] = item['material']['version']
                            paper['analysis_issues'] = item['material'].get('issues', [])
                except ValueError:
                    pass
            translated = chinese_title(paper, titles)
            if translated:
                paper['title_zh'] = translated
            try:
                paper['reading_status'] = public_status(read(DATA / 'reading-status' / (paper.get('id', '') + '.json')))
            except ValueError:
                pass
    from src.reading_notes import valid_analysis
    result['analysis_status'] = {**(result.get('analysis_status') or {}),
        'ready_core': sum(valid_analysis(p) for p in result.get('core', [])),
        'ready_extended': sum(valid_analysis(p) for p in result.get('extended', [])),
        'pending': sum(not valid_analysis(p) for p in selected(result))}
    return result


def migrate(data, payloads):
    """Idempotently recover distinct, verifiable historic runs, oldest first."""
    unique = {}
    for payload in payloads:
        if isinstance(payload, dict) and selected(payload) and parse_date(payload.get('generated_at')):
            unique.setdefault(edition_id(payload), payload)
    for payload in sorted(unique.values(), key=lambda p: parse_date(p['generated_at'])):
        append(data, payload, trigger='recovered')
    return entries(data)
