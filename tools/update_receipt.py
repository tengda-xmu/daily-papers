"""Persist the exact results accompanying a workflow's publish artifact."""
from datetime import datetime, timezone
import os
from pathlib import Path
from src.public_sources import read, write


def record(root=Path('.')):
    data = root / 'data'
    run_id = os.environ['GITHUB_RUN_ID']
    check = read(data / 'updates' / (run_id + '.json'))
    if not check:
        check = {'run_id': run_id, 'checked_at': datetime.now(timezone.utc).isoformat(),
                 'outcome': 'error' if os.getenv('PIPELINE_OUTCOME') == 'failure' else 'columns_only'}
    check['columns'] = read(data / 'public-updates.json').get('columns', {})
    write(data / 'updates' / (run_id + '.json'), check)


if __name__ == '__main__':
    record()
