"""Resumable CNS discovery, using the same journal directory as ranking.

Crossref cursors are consumed to the last page, without a request-count budget.
Each journal/query retains its results and unfinished window independently.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlencode

from src.editions import read, write
from src.models import RawRecord, SourceStatus, in_date_window, parse_date
from src.venues import cns_journals, journal_issns
from src.catalog import match_journal
from src.sources.public_literature import CrossrefAdapter

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'config/cns-search.json'
STRATEGY_VERSION = 3


class CNSJournalAdapter(CrossrefAdapter):
    # Keep the established source ID so saved filters and source history work.
    name = 'CNS 子刊专项'

    def __init__(self, config=None, queries=None, cache_dir=None, clock=None, **kwargs):
        super().__init__(queries=queries, custom_journals=[], **kwargs)
        self.config = dict(config) if config is not None else json.loads(CONFIG.read_text(encoding='utf-8'))
        self.search_queries = list(dict.fromkeys(queries if queries is not None else
            [*self.config.get('focus_queries', []), self.config.get('query', '')]))
        self.search_queries = [q.strip() for q in self.search_queries if q.strip()]
        self.journals = self.config.get('journals', cns_journals())
        # Explicit test/ad-hoc configs are in-memory unless a cache is requested.
        self.cache_dir = Path(cache_dir) if cache_dir is not None else ROOT / 'data/cache/cns' if config is None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.statistics = []
        self._memory = {}
        self._requested = False

    def _load(self, key):
        return read(self.cache_dir / (key + '.json')) if self.cache_dir else self._memory.get(key, {})

    def _save(self, key, state):
        if self.cache_dir:
            write(self.cache_dir / (key + '.json'), state)
        else:
            self._memory[key] = state

    def _request(self, params):
        if self._requested:
            time.sleep(max(0, float(self.config.get('request_interval_seconds', 2))))
        self._requested = True
        return self._get_json('https://api.crossref.org/works?' + urlencode(params), {
            'Accept': 'application/json',
            'User-Agent': 'daily-papers/1.0 (https://github.com/tengda-xmu/daily-papers)',
        })

    def _retry_at(self, exc):
        value = (getattr(exc, 'headers', None) or {}).get('Retry-After', '')
        try:
            return self.clock() + timedelta(seconds=max(0, float(value)))
        except (TypeError, ValueError):
            try:
                return parsedate_to_datetime(value).astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return self.clock() + timedelta(minutes=5)

    @staticmethod
    def _error_detail(exc):
        code = getattr(exc, 'code', None)
        detail = f'HTTP {code}' if code else str(exc)
        if code and hasattr(exc, 'read'):
            try:
                payload = json.loads(exc.read(65536).decode('utf-8'))
                messages = payload.get('message', [])
                if isinstance(messages, list):
                    explanation = '; '.join(str(m.get('type', '')) + ': ' + str(m.get('message', ''))
                                            for m in messages if isinstance(m, dict))
                else:
                    explanation = str(messages)
                if explanation:
                    detail += ': ' + explanation[:1500]
            except (OSError, ValueError, AttributeError):
                pass
        return detail

    def _window(self, state, start, until):
        if state.get('active'):
            return state['active']
        completed = parse_date(state.get('completed_until'))
        if completed and completed >= until:
            return None
        last_full = parse_date(state.get('full_completed_until'))
        full = not last_full or until - last_full >= timedelta(days=int(self.config.get('full_refresh_days', 7)))
        lower = start if full or not completed else max(start, completed - timedelta(days=int(self.config.get('overlap_days', 7))))
        return {'start': lower.isoformat(), 'until': until.isoformat(), 'kind': 'full' if full else 'incremental', 'cursor': '*'}

    def _collect_query(self, journal, query, start, until, days, provider, stat):
        issns = journal_issns(journal)
        identity = issns[0] if len(issns) == 1 else list(issns)
        key = hashlib.sha256(json.dumps([STRATEGY_VERSION, identity, query, days]).encode()).hexdigest()
        state = self._load(key)
        state.update(version=STRATEGY_VERSION, journal=journal['name'], issn=journal['issn'], issns=list(issns), query=query)
        saved = state.setdefault('records', {})
        saved = state['records'] = {k: v for k, v in saved.items() if in_date_window(v.get('published_at'), start, until)}
        if state.get('non_retryable'):
            stat['failed_queries'] += 1
            stat['errors'].append(state.get('error', 'Query configuration requires correction'))
            return list(saved.values())
        if provider.get('strategy_version') == STRATEGY_VERSION and provider.get('configuration_error'):
            stat['pending_queries'] += 1
            stat['errors'].append(provider['configuration_error'])
            return list(saved.values())
        retry = max(filter(None, (parse_date(state.get('retry_at')), parse_date(provider.get('retry_at')))), default=None)
        if retry and retry > self.clock():
            stat['pending_queries'] += 1
            stat['retry_at'] = retry.isoformat()
            return list(saved.values())
        size = max(1, min(1000, int(self.config.get('page_size', 100))))  # API page size, never a total-result limit.
        restarted = False
        while (window := self._window(state, start, until)) is not None:
            state['active'] = window
            self._save(key, state)
            try:
                issn_filters = ','.join('issn:' + value for value in journal_issns(journal))
                params = {'filter': f"{issn_filters},type:journal-article,from-pub-date:{parse_date(window['start']).date()},until-pub-date:{parse_date(window['until']).date()}",
                          'query': query, 'rows': size, 'cursor': window['cursor']}
                # Crossref rejects published/issued sorting together with a
                # cursor. Exhaust its default order, then rank dates locally.
                state['last_attempt_at'] = self.clock().isoformat()
                self._save(key, state)
                message = self._request(params).get('message')
                if not isinstance(message, dict) or not isinstance(message.get('items'), list):
                    raise ValueError('Crossref response lacks items')
                items = message['items']
                page_hash = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
                if items and page_hash == window.get('last_page_hash'):
                    raise ValueError('Crossref returned a repeated page')
                for record in self.parse_payload({'message': message}):
                    matched = match_journal(record.venue)
                    if not matched or matched['name'] != journal['name'] or not in_date_window(record.published_at, start, until):
                        continue
                    record.raw_metadata.update(query_scope=self.name, sources=['Crossref', self.name])
                    identity = record.doi or record.source_id or record.title
                    stat['_fetched'].add(identity)
                    saved[identity] = record.to_dict()
                state.pop('retry_at', None)
                state.pop('error', None)
                received = int(window.get('received', 0)) + len(items)
                total = message.get('total-results')
                if len(items) < size or (type(total) is int and received >= total):
                    state['completed_until'] = window['until']
                    if window['kind'] == 'full':
                        state['full_completed_until'] = window['until']
                    state.pop('active', None)
                else:
                    cursor = message.get('next-cursor')
                    if not cursor:
                        raise ValueError('Full Crossref page lacks next-cursor')
                    window.update(cursor=cursor, last_page_hash=page_hash, received=received)
                self._save(key, state)  # Results and the next cursor commit together.
            except Exception as exc:
                code = getattr(exc, 'code', None)
                detail = self._error_detail(exc)
                # Expired cursors restart this exact window, retaining all results.
                expired = code in (404, 410) or (code == 400 and 'cursor' in detail.lower()
                    and any(word in detail.lower() for word in ('expired', 'invalid cursor', 'cursor-invalid')))
                if expired and window['cursor'] != '*' and not restarted:
                    window.update(cursor='*')
                    window.pop('last_page_hash', None)
                    window.pop('received', None)
                    restarted = True
                    self._save(key, state)
                    continue
                retry_at = self._retry_at(exc).isoformat()
                state.update(retry_at=retry_at, error=detail)
                if code == 400:
                    state['non_retryable'] = True
                if 'sort-criteria-incompatible-with-cursor' in detail:
                    provider.update(strategy_version=STRATEGY_VERSION, configuration_error=detail)
                    self._save('provider', provider)
                if code == 429:
                    provider['retry_at'] = retry_at
                    self._save('provider', provider)
                self._save(key, state)
                stat['failed_queries'] += 1
                stat['errors'].append(state['error'])
                stat['retry_at'] = retry_at
                return list(saved.values())
        stat['completed_queries'] += 1
        return list(saved.values())

    def fetch(self, since, until):
        until = parse_date(until)
        try:
            days = max(1, int(os.getenv('CNS_LOOKBACK_DAYS') or self.config.get('lookback_days', 365)))
        except ValueError:
            days = 365
        start = until - timedelta(days=days)
        provider = self._load('provider')
        records, self.statistics = [], []
        self._requested = False
        for journal in self.journals:
            stat = {'journal': journal['name'], 'issn': journal['issn'], 'query_count': len(self.search_queries),
                    'completed_queries': 0, 'pending_queries': 0, 'failed_queries': 0, 'errors': [], '_fetched': set()}
            journal_records = {}
            for query in self.search_queries:
                for row in self._collect_query(journal, query, start, until, days, provider, stat):
                    journal_records[row.get('doi') or row.get('source_id') or row['title']] = row
            records.extend(RawRecord(**row) for row in journal_records.values())
            stat.update(fetched=len(stat.pop('_fetched')), available=len(journal_records))
            stat['status'] = 'partial' if stat['failed_queries'] or stat['pending_queries'] else 'ok'
            self.statistics.append(stat)
        records = self._finish(records, start, until)
        records.sort(key=lambda r: parse_date(r.published_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        completed = sum(s['completed_queries'] for s in self.statistics)
        total = sum(s['query_count'] for s in self.statistics)
        failures = [f"{s['journal']}: {e}" for s in self.statistics for e in s['errors']]
        pending = sum(s['pending_queries'] for s in self.statistics)
        state = ('partial' if records else 'error') if failures or pending else self._status.status
        self._status = SourceStatus(self.name, state, len(records),
            f'{completed}/{total} journal queries across {len(self.journals)} journals; window {days} days; '
            f'{pending} pending; ' + '; '.join(failures))
        return records


def main():
    import argparse
    from src.research_directions import load_profile, query_plan
    parser = argparse.ArgumentParser(description='Collect CNS metadata without publishing a recommendation batch.')
    parser.add_argument('--cache-dir', default=str(ROOT / 'data/cache/cns'))
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    until = datetime.now(timezone.utc)
    adapter = CNSJournalAdapter(queries=query_plan(load_profile())['plain'], cache_dir=args.cache_dir)
    records = adapter.fetch(until - timedelta(days=365), until)
    write(Path(args.output), {'collected_at': until.isoformat(), 'papers': [r.to_dict() for r in records],
                            'status': adapter.status.to_dict(), 'journals': adapter.statistics})
    print(json.dumps({'status': adapter.status.to_dict(), 'journals': adapter.statistics}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
