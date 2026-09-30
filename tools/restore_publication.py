"""Restore a validated public snapshot in an ephemeral publication checkout."""
import json
from pathlib import Path
import shutil
import sys


def restore(snapshot, root):
    root, snapshot = Path(root).resolve(), Path(snapshot).resolve()
    if snapshot.parent != root:
        raise ValueError('Publication snapshot must be inside this checkout')
    for file in ('data/daily.json', 'site/data.json', 'site/index.html', 'data/editions/index.json'):
        path = snapshot / file
        if not path.is_file():
            raise ValueError('Incomplete publication snapshot: ' + file)
        if path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8'))
    for name in ('data', 'site'):
        source, target = snapshot / name, root / name
        if target.is_symlink() or target.resolve().parent != root:
            raise ValueError('Invalid publication destination')
        if any(p.is_symlink() for p in source.rglob('*')):
            raise ValueError('Publication snapshots cannot contain links')
    for name in ('data', 'site'):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(snapshot / name, target)


if __name__ == '__main__':
    restore(sys.argv[1], Path.cwd())
