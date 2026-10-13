"""Refresh bounded per-paper WeChat coverage without creating a paper batch."""
from src.paper_coverage import collect


def main():
    from src.settings import load_env
    load_env()
    result = collect()
    rows = result['papers'].values()
    print('WeChat paper coverage:', sum(bool(r['articles']) for r in rows), 'matched papers')


if __name__ == '__main__':
    main()
