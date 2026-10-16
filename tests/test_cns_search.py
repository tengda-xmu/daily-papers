from datetime import datetime, timedelta, timezone
import json
from urllib.parse import urlsplit, parse_qs

from src.sources.cns_journals import CNSJournalAdapter

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)


def fixture_config(**values):
    return {'lookback_days': 365, 'query': 'robotics', 'page_size': 2, 'request_interval_seconds': 0,
            'journals': [{'name': 'Nature', 'issn': '1476-4687'}], **values}


def item(identifier, date=(2026, 10, 1), venue='Nature'):
    return {'title': ['Robotics ' + str(identifier)], 'DOI': '10.test/' + str(identifier),
            'container-title': [venue], 'published': {'date-parts': [list(date)]}}


def page(*items, cursor=None):
    return {'message': {'items': list(items), **({'next-cursor': cursor} if cursor else {})}}


def test_cns_search_scopes_journal_date_and_rejects_wrong_venue(monkeypatch):
    config = {"lookback_days": 180, "query": "structural reliability", "journals": [{"name": "Nature Communications", "issn": "2041-1723"}]}
    a = CNSJournalAdapter(config=config)
    calls = []
    def response(url, headers):
        calls.append(url)
        return {"message": {"items": [
            {"title": ["Relevant"], "DOI": "10/a", "container-title": ["Nature Communications"], "published": {"date-parts": [[2026, 8, 1]]}},
            {"title": ["Wrong journal"], "DOI": "10/b", "container-title": ["Other"], "published": {"date-parts": [[2026, 8, 1]]}},
            {"title": ["Future"], "DOI": "10/c", "container-title": ["Nature Communications"], "published": {"date-parts": [[2027, 8, 1]]}},
        ]}}
    monkeypatch.delenv("CNS_LOOKBACK_DAYS", raising=False)
    monkeypatch.setattr(a, "_get_json", response)
    rows = a.fetch(datetime(2026, 8, 1), datetime(2026, 9, 23))
    assert len(rows) == 1 and rows[0].raw_metadata["query_scope"] == a.name
    assert "issn:2041-1723" in parse_qs(urlsplit(calls[0]).query)["filter"][0]
    assert "from-pub-date:2026-03-27" in calls[0].replace("%3A", ":")
    assert a.status.status == "ok"


def test_partial_cns_failure_keeps_other_journal_results(monkeypatch):
    from urllib.error import HTTPError
    config = {"lookback_days": 180, "query": "structural", "journals": [
        {"name": "Nature Communications", "issn": "2041-1723"},
        {"name": "Science Advances", "issn": "2375-2548"}]}
    a = CNSJournalAdapter(config=config)
    def respond(url, headers):
        if "2375-2548" in url:
            raise HTTPError(url, 503, "unavailable", {}, None)
        return {"message": {"items": [{"title": ["Machine learning structural reliability"], "DOI": "10/a",
                                       "container-title": ["Nature Communications"], "published": {"date-parts": [[2026, 8, 1]]}}]}}
    monkeypatch.delenv("CNS_LOOKBACK_DAYS", raising=False)
    monkeypatch.setattr(a, "_get_json", respond)
    monkeypatch.setattr("src.sources.cns_journals.time.sleep", lambda _: None)
    records = a.fetch(datetime(2026, 8, 1), datetime(2026, 9, 23))
    assert len(records) == 1 and a.status.status == "partial"
    assert "1/2 journal queries" in a.status.message and "HTTP 503" in a.status.message


def test_cns_search_runs_agent_queries_before_general_queries(monkeypatch):
    config = {"lookback_days": 180, "query": "structural fatigue",
              "focus_queries": ["large language model multi-agent"],
              "journals": [{"name": "npj Artificial Intelligence", "issn": "3005-1460"}]}
    a = CNSJournalAdapter(config=config)
    queries = []
    def respond(url, headers):
        queries.append(parse_qs(urlsplit(url).query)["query"][0])
        return {"message": {"items": []}}
    monkeypatch.setattr(a, "_get_json", respond)
    monkeypatch.setattr("src.sources.cns_journals.time.sleep", lambda _: None)
    a.fetch(datetime(2026, 8, 1), datetime(2026, 9, 23))
    assert queries == ["large language model multi-agent", "structural fatigue"]


def test_full_cursor_pagination_has_no_request_count_cutoff(monkeypatch, tmp_path):
    adapter = CNSJournalAdapter(config=fixture_config(page_size=1), cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    def respond(url, headers):
        params = parse_qs(urlsplit(url).query)
        calls.append(params)
        index = len(calls)
        return page(item(index), cursor='page-' + str(index)) if index <= 35 else page()
    monkeypatch.setattr(adapter, '_get_json', respond)
    rows = adapter.fetch(NOW, NOW)
    assert len(rows) == 35 and len(calls) == 36
    assert calls[0]['cursor'] == ['*'] and calls[-1]['cursor'] == ['page-35']
    assert all('sort' not in c and 'order' not in c for c in calls)
    assert 'from-pub-date:2025-10-15' in calls[0]['filter'][0]
    assert adapter.statistics[0]['completed_queries'] == 1


def test_partial_page_persists_results_cursor_and_resumes(monkeypatch, tmp_path):
    adapter = CNSJournalAdapter(config=fixture_config(), cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    def interrupted(url, headers):
        cursor = parse_qs(urlsplit(url).query)['cursor'][0]
        calls.append(cursor)
        if cursor == '*': return page(item(1), item(2), cursor='next')
        raise TimeoutError('fixture timeout')
    monkeypatch.setattr(adapter, '_get_json', interrupted)
    assert len(adapter.fetch(NOW, NOW)) == 2 and calls == ['*', 'next']
    assert adapter.status.status == 'partial'
    state = json.loads(next(p for p in tmp_path.glob('*.json') if p.name != 'provider.json').read_text())
    assert state['active']['cursor'] == 'next' and not state.get('completed_until')
    restarted = CNSJournalAdapter(config=fixture_config(), cache_dir=tmp_path, clock=lambda: NOW + timedelta(minutes=6))
    resumed = []
    def response(url, headers):
        resumed.append(parse_qs(urlsplit(url).query)['cursor'][0])
        return page(item(3))
    monkeypatch.setattr(restarted, '_get_json', response)
    assert len(restarted.fetch(NOW, NOW)) == 3 and resumed == ['next']
    assert restarted.statistics[0]['completed_queries'] == 1
    assert len(restarted.fetch(NOW, NOW)) == 3 and resumed == ['next']


def test_incremental_overlap_weekly_full_and_new_query_invalidation(monkeypatch, tmp_path):
    config = fixture_config()
    def run(when, query='robotics'):
        adapter = CNSJournalAdapter(config=config, queries=[query], cache_dir=tmp_path, clock=lambda: when)
        calls = []
        def response(url, headers):
            calls.append(parse_qs(urlsplit(url).query)['filter'][0]); return page(item(1))
        monkeypatch.setattr(adapter, '_get_json', response)
        assert len(adapter.fetch(when, when)) == 1
        return calls
    assert 'from-pub-date:2025-10-15' in run(NOW)[0]
    assert 'from-pub-date:2026-10-08' in run(NOW + timedelta(days=1))[0]
    assert 'from-pub-date:2025-10-23' in run(NOW + timedelta(days=8))[0]
    assert 'from-pub-date:2025-10-23' in run(NOW + timedelta(days=8), 'new direction')[0]


def test_provider_429_keeps_results_and_marks_unattempted_queries_pending(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    config = fixture_config(journals=[{'name': 'Nature', 'issn': '1476-4687'}, {'name': 'Science', 'issn': '1095-9203'}])
    adapter = CNSJournalAdapter(config=config, queries=['one', 'two'], cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    def response(url, headers):
        calls.append(url)
        if len(calls) == 1: return page(item(1))
        raise HTTPError(url, 429, 'slow down', {'Retry-After': '3600'}, None)
    monkeypatch.setattr(adapter, '_get_json', response)
    assert len(adapter.fetch(NOW, NOW)) == 1 and len(calls) == 2
    assert adapter.statistics[0]['failed_queries'] == 1
    assert adapter.statistics[0]['errors'] == ['HTTP 429']
    assert adapter.statistics[1]['pending_queries'] == 2 and adapter.status.status == 'partial'
    assert len(adapter.fetch(NOW, NOW)) == 1 and len(calls) == 2
    later = CNSJournalAdapter(config=config, queries=['one', 'two'], cache_dir=tmp_path, clock=lambda: NOW + timedelta(hours=2))
    monkeypatch.setattr(later, '_get_json', lambda url, headers: page())
    assert len(later.fetch(NOW, NOW)) == 1
    assert sum(s['completed_queries'] for s in later.statistics) == 4


def test_expired_cursor_restarts_same_window_without_losing_saved_rows(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    adapter = CNSJournalAdapter(config=fixture_config(page_size=1), cache_dir=tmp_path, clock=lambda: NOW)
    responses = [page(item(1), cursor='expired'), TimeoutError('interruption')]
    def first(url, headers):
        value = responses.pop(0)
        if isinstance(value, Exception): raise value
        return value
    monkeypatch.setattr(adapter, '_get_json', first)
    assert len(adapter.fetch(NOW, NOW)) == 1
    resumed = CNSJournalAdapter(config=fixture_config(page_size=1), cache_dir=tmp_path, clock=lambda: NOW + timedelta(minutes=6))
    calls = []
    def response(url, headers):
        cursor = parse_qs(urlsplit(url).query)['cursor'][0]; calls.append(cursor)
        if cursor == 'expired':
            import io
            raise HTTPError(url, 400, 'expired cursor', {}, io.BytesIO(json.dumps({'message': [{'type': 'cursor-invalid', 'message': 'Expired cursor'}]}).encode()))
        return page(item(2), cursor='fresh') if cursor == '*' else page()
    monkeypatch.setattr(resumed, '_get_json', response)
    assert len(resumed.fetch(NOW, NOW)) == 2 and calls == ['expired', '*', 'fresh']


def test_repeated_pages_are_failure_not_success_or_infinite_loop(monkeypatch, tmp_path):
    adapter = CNSJournalAdapter(config=fixture_config(page_size=1), cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    def response(url, headers):
        calls.append(url); return page(item(1), cursor='same')
    monkeypatch.setattr(adapter, '_get_json', response)
    assert len(adapter.fetch(NOW, NOW)) == 1 and len(calls) == 2
    assert adapter.statistics[0]['failed_queries'] == 1
    assert 'repeated page' in adapter.status.message


def test_main_journal_accepted_only_for_its_own_query_and_valid_dates(monkeypatch):
    adapter = CNSJournalAdapter(config=fixture_config(page_size=100), clock=lambda: NOW)
    monkeypatch.setattr(adapter, '_get_json', lambda url, headers: page(item(1), item(2, venue='Science'),
        item(3, (2024, 1, 1)), item(4, (2027, 1, 1)), item(5, venue='Nature Communications')))
    assert [r.doi for r in adapter.fetch(NOW, NOW)] == ['10.test/1']


def test_real_crossref_sort_cursor_validation_is_reported_and_not_retried(monkeypatch, tmp_path):
    import io
    from urllib.error import HTTPError
    config = fixture_config(journals=[{'name': 'Nature', 'issn': '1476-4687'}, {'name': 'Science', 'issn': '1095-9203'}])
    adapter = CNSJournalAdapter(config=config, queries=['robotics', 'fault diagnosis'], cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    error_body = {'status': 'failed', 'message-type': 'validation-failure', 'message': [{
        'type': 'sort-criteria-incompatible-with-cursor', 'value': 'published',
        'message': 'Sorting by [issued,published,published-print,published-online] is not supported when using a cursor.'}]}
    def response(url, headers):
        calls.append(url)
        # Actual request has the fix; fixture preserves the production failure
        # should the provider ever reject a future parameter combination.
        assert 'sort' not in parse_qs(urlsplit(url).query)
        raise HTTPError(url, 400, 'Bad Request', {}, io.BytesIO(json.dumps(error_body).encode()))
    monkeypatch.setattr(adapter, '_get_json', response)
    assert adapter.fetch(NOW, NOW) == [] and len(calls) == 1
    assert 'sort-criteria-incompatible-with-cursor' in adapter.status.message
    assert sum(s['pending_queries'] for s in adapter.statistics) == 3
    assert sum(s['failed_queries'] for s in adapter.statistics) == 1
    assert adapter.fetch(NOW, NOW) == [] and len(calls) == 1


def test_same_cursor_token_can_advance_pages_and_results_sort_locally(monkeypatch):
    adapter = CNSJournalAdapter(config=fixture_config(page_size=1), clock=lambda: NOW)
    responses = [page(item('old', (2026, 5, 1)), cursor='same-token'),
                 page(item('new', (2026, 10, 1)), cursor='same-token'), page()]
    monkeypatch.setattr(adapter, '_get_json', lambda url, headers: responses.pop(0))
    assert [r.doi for r in adapter.fetch(NOW, NOW)] == ['10.test/new', '10.test/old']
    assert adapter.statistics[0]['completed_queries'] == 1


def test_cell_print_issn_is_queried_and_new_alias_invalidates_old_empty_cache(monkeypatch, tmp_path):
    config = fixture_config(journals=[{'name': 'Cell', 'issn': '1097-4172'}])
    adapter = CNSJournalAdapter(config=config, cache_dir=tmp_path, clock=lambda: NOW)
    monkeypatch.setattr(adapter, '_get_json', lambda url, headers: page())
    assert adapter.fetch(NOW, NOW) == []
    # Crossref's Cell metadata can list only the print ISSN. Adding that alias
    # must run a full query immediately even at the same timestamp.
    config['journals'][0]['issns'] = ['1097-4172', '0092-8674']
    fixed = CNSJournalAdapter(config=config, cache_dir=tmp_path, clock=lambda: NOW)
    calls = []
    def response(url, headers):
        filters = parse_qs(urlsplit(url).query)['filter'][0]; calls.append(filters)
        assert 'issn:1097-4172' in filters and 'issn:0092-8674' in filters
        return page(item('cell', venue='Cell'))
    monkeypatch.setattr(fixed, '_get_json', response)
    assert len(fixed.fetch(NOW, NOW)) == 1 and len(calls) == 1
    assert 'from-pub-date:2025-10-15' in calls[0]


def test_exact_full_last_page_uses_total_when_cursor_is_absent(monkeypatch):
    adapter = CNSJournalAdapter(config=fixture_config(), clock=lambda: NOW)
    monkeypatch.setattr(adapter, '_get_json', lambda url, headers: {'message': {
        'items': [item(1), item(2)], 'total-results': 2}})
    assert len(adapter.fetch(NOW, NOW)) == 2
    assert adapter.statistics[0]['completed_queries'] == 1
