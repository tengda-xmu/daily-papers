"""Independently refresh public columns, and import validated AI reading notes."""
import argparse
from datetime import datetime, timezone, timedelta
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {'ai': 'ai-updates.json', 'opportunities': 'opportunities.json',
         'leads': 'research-leads.json', 'social': 'social-articles.json'}


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def pending_columns(root=ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    ledger = load(root / 'data/public-updates.json').get('columns', {})
    return [name for name, file in FILES.items()
            if column_needed(ledger.get(name) or load(root / 'data' / file), now)]


def import_readings(root=ROOT):
    from src.public_sources import read, write
    from src.ai_updates import validate_analysis, public_index
    index = public_index(root)
    rows = {r['id']: r for r in index.get('entries', []) + index.get('social_readings', [])}
    result = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'origin/connector-data', '--', 'data/ai-readings'],
                            cwd=root, capture_output=True, text=True)
    count = 0
    for path in result.stdout.splitlines():
        identifier = path.rsplit('/', 1)[-1].removesuffix('.json')
        if identifier not in rows or path != 'data/ai-readings/' + identifier + '.json':
            continue
        output = subprocess.run(['git', 'show', 'origin/connector-data:' + path], cwd=root, capture_output=True)
        try:
            value = validate_analysis(json.loads(output.stdout), rows[identifier])
        except (ValueError, TypeError, AttributeError):
            continue
        write(root / path, value)
        count += 1
    return count


def needed(root=ROOT, now=None):
    return bool(pending_columns(root, now))


def column_needed(payload, now):
    from src.update_cycle import in_cycle
    outcome = payload.get('outcome') or ('partial' if any(
        s.get('status') not in ('ok', 'no_data') for s in payload.get('sources', [])) else 'ok')
    if in_cycle(payload.get('checked_at'), now) and outcome == 'ok':
        return False
    try:
        retry = datetime.fromisoformat(payload.get('next_retry_at', '').replace('Z', '+00:00'))
        if in_cycle(payload.get('checked_at'), now) and retry.tzinfo and retry > now:
            return False
    except (ValueError, TypeError):
        pass
    return True


def refresh(root=ROOT, *, due_only=False, now=None):
    from src.ai_updates import refresh as ai
    from src.opportunities import refresh as opportunities
    from src.research_leads import refresh as leads
    from concurrent.futures import ThreadPoolExecutor
    from src.public_sources import write
    import os
    now = now or datetime.now(timezone.utc)
    previous = load(root / 'data/public-updates.json').get('columns', {})
    selected = set(pending_columns(root, now)) if due_only else set(FILES)
    state = {'run_id': os.environ.get('GITHUB_RUN_ID', ''), 'checked_at': now.isoformat(), 'columns': dict(previous)}
    def record(name, value):
        from src.update_cycle import in_cycle, retry_delay
        old = previous.get(name, {})
        count = 0 if value['outcome'] == 'ok' else (old.get('failures', 0) if in_cycle(old.get('checked_at'), now) else 0) + 1
        value.update(checked_at=value.get('checked_at') or now.isoformat(), failures=count)
        if count:
            value['next_retry_at'] = (now + timedelta(seconds=retry_delay(count))).isoformat()
        state['columns'][name] = value
    from src.social_content import refresh as social_refresh
    try:
        if 'social' in selected:
            social = social_refresh(root)
            record('social', {k:social[k] for k in ('outcome', 'checked_at', 'sources')})
            state['columns']['social']['counts'] = {name:sum(r.get('column') == name for r in social['entries']) for name in ('ai','leads')}
    except Exception:
        record('social', {'outcome':'error', 'retained':True})
    # Each column owns a separate file and failure state; papers are untouched.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn, root) for name, fn in [('ai', ai), ('opportunities', opportunities), ('leads', leads)] if name in selected}
        for name, future in futures.items():
            try:
                result = future.result()
                record(name, {'outcome': result.get('outcome', 'partial' if any(s.get('status') == 'error' for s in result.get('sources', [])) else 'ok'),
                                          'checked_at': result.get('checked_at'), 'count': len(result.get('entries', []))}
                )
                print(name + ': ' + str(len(result.get('entries', []))) + ' entries; ' + result.get('outcome', 'checked'), flush=True)
            except Exception as exc:
                record(name, {'outcome': 'error', 'retained': True})
                print(name + ': failed (' + type(exc).__name__ + '), previous records retained', flush=True)
    write(root / 'data/public-updates.json', state)
    import_readings(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--import-readings', action='store_true')
    parser.add_argument('--due-only', action='store_true')
    args = parser.parse_args()
    if args.refresh:
        refresh(due_only=args.due_only)
    if args.import_readings:
        print('AI readings imported:', import_readings())


if __name__ == '__main__':
    main()
