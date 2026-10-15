"""Move only processed coverage metadata between independent collection/publication.

Publication merges into the checkout's current papers; it never restores a
collector's old recommendation, reading, HTML, or site snapshot.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path

from src.coverage_evidence import PUBLIC, public_mirrors, public_review
from src.models import parse_date
from src.paper_coverage import (DATA, SCHEMA_VERSION, _body_checked_at, _project, load_public,
                                permissions, selected_papers)
from src.public_sources import ROOT, read, write


def _receipts(directory):
    if Path(directory).is_symlink():
        raise ValueError('Coverage transfer cannot contain symbolic links')
    result = []
    for path in sorted(Path(directory).glob('*.json')):
        if path.is_symlink():
            raise ValueError('Coverage transfer cannot contain symbolic links')
        receipt = public_review(read(path))
        if path.stem != receipt['id']:
            raise ValueError('Coverage review filename does not match its identity')
        result.append(receipt)
    return result


def export_results(destination, root=ROOT):
    """Reproject before upload, excluding all private collection/cache fields."""
    root, destination = Path(root), Path(destination)
    if destination.is_symlink():
        raise ValueError('Coverage destination cannot be a symbolic link')
    receipts = _receipts(root / PUBLIC)
    payload = read(root / DATA)
    write(destination / 'paper-coverage.json', {
        'version': SCHEMA_VERSION, 'checked_at': payload.get('checked_at', ''),
        'papers': load_public(root)})
    for receipt in receipts:
        write(destination / 'coverage-reviews' / (receipt['id'] + '.json'), receipt)
    return len(receipts)


def _newer(incoming, existing, field='last_attempt_at'):
    left, right = parse_date(incoming.get(field)), parse_date(existing.get(field))
    return bool(left and (not right or left > right))


def _merge_article(existing, incoming, *, newer_record, existing_attempt=None, incoming_attempt=None):
    """Merge sanitized article snapshots, not identity-only review receipts.

    A new failed read changes accessibility, not the previously proved identity
    or saved content. Actual per-source read times take precedence over record
    times; replacing readable content also requires current identity evidence.
    """
    if not existing:
        return deepcopy(incoming)
    result = deepcopy(existing)
    incoming_verified = parse_date(incoming.get('verified_at'))
    existing_verified = parse_date(existing.get('verified_at'))
    identity_current = (not existing_verified or
                        bool(incoming_verified and incoming_verified >= existing_verified))
    actual_incoming = parse_date(incoming.get('body_checked_at'))
    old_body = _body_checked_at(existing) or parse_date(existing_attempt)
    if actual_incoming:
        body_current = (not old_body or actual_incoming > old_body
                        or (actual_incoming == old_body and newer_record))
    else:
        # Legacy transfers lack a per-source observation. Keep their original
        # record gate, but never let an older readable verification win either.
        legacy_incoming = _body_checked_at(incoming) or parse_date(incoming_attempt)
        body_current = newer_record and (not old_body or bool(legacy_incoming and legacy_incoming >= old_body))
    if body_current:
        if incoming.get('body_status') == 'readable' and identity_current:
            # These complete records already passed _project. In particular,
            # adopt the new summary/version and authorized body together; an
            # identity-only merge would incorrectly retain the old body here.
            result = deepcopy(incoming)
        elif incoming.get('body_status') in ('challenge', 'unavailable', 'pending'):
            result['body_status'] = incoming['body_status']
            if actual_incoming:
                result['body_checked_at'] = incoming['body_checked_at']
    mirrors = public_mirrors(existing.get('mirrors', []) + incoming.get('mirrors', []))
    if mirrors:
        result['mirrors'] = mirrors
    return result


def _merge_record(existing, incoming):
    """Retain concurrent discoveries while stale scans cannot roll metadata back."""
    newer = _newer(incoming, existing)
    result = deepcopy(incoming if not existing or newer else existing)
    articles = {row['url']: row for row in existing.get('articles', [])}
    for row in incoming.get('articles', []):
        articles[row['url']] = _merge_article(articles.get(row['url'], {}), row,
            newer_record=not existing or newer, existing_attempt=existing.get('last_attempt_at'),
            incoming_attempt=incoming.get('last_attempt_at'))
    # An old snapshot may contain a candidate the current checkout has already
    # reviewed or discarded. Only a newer scan can contribute pending leads.
    candidates = {row['url']: row for row in existing.get('candidates', [])}
    if not existing or newer:
        for row in incoming.get('candidates', []):
            prior = candidates.get(row['url'])
            if not prior or _newer(row, prior) or not prior.get('last_attempt_at'):
                candidates[row['url']] = row
    result.update(articles=list(articles.values()),
                  candidates=[row for url, row in candidates.items() if url not in articles])
    providers = deepcopy(existing.get('search_providers', {}))
    for provider, detail in incoming.get('search_providers', {}).items():
        if provider not in providers or _newer(detail, providers[provider], 'checked_at'):
            providers[provider] = detail
    if providers:
        result['search_providers'] = providers
    # Reading a source later does not make an older search/strategy current.
    # Compare search timestamps separately from the overall attempt timestamp.
    incoming_search = (_newer(incoming, existing, 'last_search_at')
                       and incoming.get('search_version', 0) >= existing.get('search_version', 0))
    if existing and not incoming_search:
        for field in ('last_search_at', 'next_search_at', 'search_status', 'search_version'):
            if field in existing:
                result[field] = existing[field]
            else:
                result.pop(field, None)
    if articles:
        result['state'] = 'matched'
        verified = [row['verified_at'] for row in articles.values() if parse_date(row.get('verified_at'))]
        if verified:
            result['verified_at'] = max(verified, key=parse_date)
    elif result['candidates'] and result.get('state') == 'no_match':
        result['state'] = 'pending_match'
    return result


def merge_results(source, root=ROOT):
    """Run after acquiring the publishing lock and checking out latest main."""
    root, source = Path(root), Path(source)
    payload_path = source / 'paper-coverage.json'
    if source.is_symlink() or payload_path.is_symlink():
        raise ValueError('Coverage transfer cannot contain symbolic links')
    payload = read(payload_path)
    if (payload.get('version') != SCHEMA_VERSION or not isinstance(payload.get('papers'), dict)):
        raise ValueError('Invalid public coverage transfer')
    # Validate every receipt before changing the checkout.
    receipts = _receipts(source / 'coverage-reviews')
    papers = {paper['id'] for paper in selected_papers(root)}
    for receipt in receipts:
        if receipt['paper_id'] not in papers:
            continue
        destination = root / PUBLIC / (receipt['id'] + '.json')
        old = read(destination)
        if old:
            old = public_review(old)
        if not old or _newer(receipt['article'], old['article'], 'verified_at'):
            write(destination, receipt)
    current_payload = read(root / DATA)
    current = load_public(root)
    incoming = _project(payload, permissions(root), root=root)
    for paper_id, record in incoming.items():
        if paper_id in papers:
            current[paper_id] = _merge_record(current.get(paper_id, {}), record)
    stamps = [stamp for stamp in (current_payload.get('checked_at'), payload.get('checked_at')) if parse_date(stamp)]
    write(root / DATA, {'version': SCHEMA_VERSION,
                       'checked_at': max(stamps, key=parse_date) if stamps else '',
                       'papers': _project({'papers': current}, permissions(root), root=root)})
    # A completed review may exist even if collection stopped before this paper.
    from tools.import_coverage_reviews import apply_reviews
    apply_reviews(root)
    return len(set(incoming) & papers)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--export', action='store_true', help='Export sanitized collection results instead of merging.')
    args = parser.parse_args(argv)
    count = export_results(args.directory) if args.export else merge_results(args.directory)
    print(('Exported reviewed sources:' if args.export else 'Merged coverage records:'), count)


if __name__ == '__main__':
    main()
