"""Recover published editions and import whitelisted reading enrichment."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from src.editions import append, entries, enrich, migrate, read, relative_path, selected, write
from src.auto_reading import load, public_analysis

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = 'https://tengda-xmu.github.io/daily-papers/'


def fetch(path):
    with urlopen(Request(PUBLIC + path, headers={'Cache-Control': 'no-cache'}), timeout=20) as response:
        body = response.read(20_000_001)
    if len(body) > 20_000_000:
        raise ValueError('Public recommendation file too large')
    return json.loads(body)


def reconcile(data, get=fetch):
    """The deployed manifest wins if a post-deploy Git commit previously failed."""
    try:
        remote = get('editions/index.json')
    except HTTPError as exc:
        if exc.code == 404:
            return
        raise
    local = {e['id'] for e in entries(data)}
    for entry in remote.get('editions', []):
        path = relative_path(entry)
        if entry['id'] in local:
            continue
        payload = get(path)
        if payload.get('edition') != entry or not selected(payload):
            raise ValueError('Published edition failed identity validation')
        append(data, payload, trigger=entry['trigger'], day=entry['date'])
    if remote.get('editions'):
        latest = max(remote['editions'], key=lambda e: (e['date'], e['number']))
        current = read(data / 'daily.json')
        if current.get('edition', {}).get('id') != latest['id']:
            restored = read(data / relative_path(latest))
            if restored:
                write(data / 'daily.json', restored)


def recover(root=ROOT, *, git_history=False):
    data = root / 'data'
    marker = data / 'editions/migration.json'
    if marker.exists():
        return
    backup = root / '.local' / ('recommendations-before-editions-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True, exist_ok=True)
    for name in ('daily.json', 'archive'):
        source = data / name
        if source.is_dir():
            shutil.copytree(source, backup / name)
        elif source.exists():
            shutil.copy2(source, backup / name)
    payloads = [read(p) for p in [data / 'daily.json', *sorted((data / 'archive').glob('*.json')),
                *sorted((root / '.local/codex-bridge/recommendation-history').glob('*.json'))]]
    if git_history:
        commits = subprocess.run(['git', 'log', '--format=%H', '--', 'data/archive'], cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
        for commit in commits:
            paths = subprocess.run(['git', 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', commit, '--', 'data/archive'],
                                   cwd=root, capture_output=True, text=True, check=True).stdout.splitlines()
            for path in paths:
                if not path.endswith('.json'):
                    continue
                result = subprocess.run(['git', 'show', f'{commit}:{path}'], cwd=root, capture_output=True)
                try:
                    payloads.append(json.loads(result.stdout))
                except ValueError:
                    pass
    recovered = migrate(data, payloads)
    current = read(data / 'daily.json')
    from src.editions import edition_id
    match = next((e for e in recovered if e['id'] == edition_id(current)), None)
    if match:
        current['edition'] = match
        write(data / 'daily.json', current)
    write(marker, {'schema': 1, 'recovered': len(recovered), 'migrated_at': datetime.now(timezone.utc).isoformat()})


def import_readings(root=ROOT):
    result = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'origin/connector-data', '--', 'data/auto-reading'],
                            cwd=root, capture_output=True, text=True)
    if result.returncode:
        return 0
    count = 0
    for path in result.stdout.splitlines():
        if not path.startswith('data/auto-reading/') or not path.endswith('.json'):
            continue
        raw = subprocess.run(['git', 'show', f'origin/connector-data:{path}'], cwd=root, capture_output=True, check=True).stdout
        try:
            item = public_analysis(json.loads(raw))
        except (ValueError, TypeError, AttributeError):
            continue
        target = root / 'data/auto-reading' / (item['paper_id'] + '.json')
        write(target, item)
        count += 1
    return count


def enrich_all(data):
    analyses = load(data)
    for path in [data / 'daily.json', *sorted((data / 'archive').glob('*.json')),
                 *(data / relative_path(e) for e in entries(data))]:
        payload = read(path)
        if payload:
            write(path, enrich(payload, analyses))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--reconcile', action='store_true')
    parser.add_argument('--import-readings', action='store_true')
    args = parser.parse_args()
    if args.recover:
        recover(git_history=True)
    if args.reconcile:
        reconcile(ROOT / 'data')
    if args.import_readings:
        print('Imported validated public analyses:', import_readings())
        enrich_all(ROOT / 'data')


if __name__ == '__main__':
    main()
