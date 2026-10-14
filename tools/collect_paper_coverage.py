"""Refresh all due paper introductions without creating a paper batch."""
import argparse
from collections import Counter
import sys

from src.paper_coverage import collect


def report_progress(done, total, paper, record):
    title = paper.get('title_zh') or paper.get('title') or paper['id']
    title = ' '.join(str(title).split())
    print(f'[{done}/{total}] {record["state"]}: {title}', flush=True)


def main(argv=None):
    # Windows redirected stdout may default to GBK, which cannot represent
    # scientific paper titles. Logging must not interrupt a completed search.
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if callable(reconfigure):
        reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true',
                        help='Recheck selected papers now instead of reusing current search and source caches.')
    parser.add_argument('--paper-id', action='append', dest='paper_ids',
                        help='Recheck this stable paper ID; repeat to select several papers (default: all due papers).')
    args = parser.parse_args(argv)
    from src.settings import load_env
    load_env()
    result = collect(force_refresh=args.refresh, paper_ids=args.paper_ids,
                     progress_callback=report_progress)
    rows = result['papers'].values()
    counts = Counter(row['state'] for row in rows)
    print('WeChat paper coverage:', ', '.join(f'{state}={count}' for state, count in sorted(counts.items())))
    deferred = Counter(row.get('deferred_reason') or 'queued' for row in rows if row['state'] == 'deferred')
    if deferred:
        print('Deferred reasons:', ', '.join(f'{reason}={count}' for reason, count in sorted(deferred.items())))


if __name__ == '__main__':
    main()
