"""Independently refresh public columns, and import validated AI reading notes."""
import argparse
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def import_readings(root=ROOT):
    from src.public_sources import read, write
    from src.ai_updates import validate_analysis
    rows = {r['id']: r for r in read(root / 'data/ai-updates.json').get('entries', [])}
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
    now = now or datetime.now(timezone.utc)
    from tools.daily_schedule import BEIJING
    start = now.astimezone(BEIJING).replace(hour=21, minute=0, second=0, microsecond=0)
    if now < start:
        return False
    for name in ('ai-updates.json', 'opportunities.json'):
        try:
            payload = json.loads((root / 'data' / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            payload = {}
        if column_needed(payload, now):
            return True
    return False


def column_needed(payload, now):
    from tools.daily_schedule import BEIJING
    start = now.astimezone(BEIJING).replace(hour=21, minute=0, second=0, microsecond=0)
    if now < start:
        return False
    try:
        checked = datetime.fromisoformat(payload.get('checked_at', '').replace('Z', '+00:00'))
        return not (checked.tzinfo and start <= checked <= now and payload.get('outcome') == 'ok')
    except (ValueError, TypeError):
        return True


def refresh(root=ROOT):
    from src.ai_updates import refresh as ai
    from src.opportunities import refresh as opportunities
    from src.research_leads import refresh as leads
    from concurrent.futures import ThreadPoolExecutor
    from src.public_sources import write
    import os
    state = {'run_id': os.environ.get('GITHUB_RUN_ID', ''), 'checked_at': datetime.now(timezone.utc).isoformat(), 'columns': {}}
    # Each column owns a separate file and failure state; papers are untouched.
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn, root) for name, fn in [('ai', ai), ('opportunities', opportunities), ('leads', leads)]}
        for name, future in futures.items():
            try:
                result = future.result()
                state['columns'][name] = {'outcome': result.get('outcome', 'partial' if any(s.get('status') == 'error' for s in result.get('sources', [])) else 'ok'),
                                          'checked_at': result.get('checked_at'), 'count': len(result.get('entries', []))}
                print(name + ': ' + str(len(result.get('entries', []))) + ' entries; ' + result.get('outcome', 'checked'), flush=True)
            except Exception as exc:
                state['columns'][name] = {'outcome': 'error', 'retained': True}
                print(name + ': failed (' + type(exc).__name__ + '), previous records retained', flush=True)
    write(root / 'data/public-updates.json', state)
    import_readings(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--import-readings', action='store_true')
    args = parser.parse_args()
    if args.refresh:
        refresh()
    if args.import_readings:
        print('AI readings imported:', import_readings())


if __name__ == '__main__':
    main()
