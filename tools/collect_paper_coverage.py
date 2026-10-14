"""Refresh bounded per-paper WeChat coverage without creating a paper batch."""
from collections import Counter

from src.paper_coverage import collect


def main():
    from src.settings import load_env
    load_env()
    result = collect()
    rows = result['papers'].values()
    counts = Counter(row['state'] for row in rows)
    print('WeChat paper coverage:', ', '.join(f'{state}={count}' for state, count in sorted(counts.items())))
    deferred = Counter(row.get('deferred_reason') or 'queued' for row in rows if row['state'] == 'deferred')
    if deferred:
        print('Deferred reasons:', ', '.join(f'{reason}={count}' for reason, count in sorted(deferred.items())))


if __name__ == '__main__':
    main()
