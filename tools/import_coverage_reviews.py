"""Import sanitized local image reviews before static publication (no fetching)."""
from pathlib import Path
import subprocess

from src.coverage_evidence import PUBLIC, load_reviews, merge_review, public_review
from src.models import parse_date
from src.public_sources import read, write

ROOT = Path(__file__).resolve().parents[1]


def apply_reviews(root=ROOT):
    from src.paper_coverage import DATA, SCHEMA_VERSION, selected_papers, _project, permissions
    path = Path(root) / DATA
    payload = read(path)
    rows = payload.setdefault('papers', {})
    count = 0
    for paper in selected_papers(root):
        approvals = load_reviews(root, paper)
        if not approvals:
            continue
        record = rows.setdefault(paper['id'], {'articles': [], 'candidates': []})
        articles = {row['url']: row for row in record.get('articles', [])}
        for article in approvals:
            old = articles.get(article['url'], {})
            articles[article['url']] = merge_review(old, article)
            count += 1
        record.update(state='matched', articles=list(articles.values()),
                      candidates=[c for c in record.get('candidates', []) if c.get('url') not in articles])
        verified = [a['verified_at'] for a in articles.values() if parse_date(a.get('verified_at'))]
        if verified:
            record['verified_at'] = max(verified, key=parse_date)
    payload['version'] = SCHEMA_VERSION
    payload['papers'] = _project(payload, permissions(root), root=root)
    write(path, payload)
    return count


def import_reviews(root=ROOT):
    result = subprocess.run(['git', 'ls-tree', '-r', '--name-only', 'origin/connector-data', '--', PUBLIC],
                            cwd=root, capture_output=True, text=True, check=True)
    imported = 0
    for path in result.stdout.splitlines():
        if not path.startswith(PUBLIC + '/') or not path.endswith('.json'):
            continue
        raw = subprocess.run(['git', 'show', f'origin/connector-data:{path}'], cwd=root,
                             capture_output=True, check=True).stdout
        try:
            import json
            receipt = public_review(json.loads(raw))
        except (ValueError, TypeError, AttributeError):
            continue
        write(Path(root) / PUBLIC / (receipt['id'] + '.json'), receipt)
        imported += 1
    apply_reviews(root)
    return imported


if __name__ == '__main__':
    print('Imported verified public-account associations:', import_reviews())
