import asyncio
import base64
from datetime import date, datetime, timezone
import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.journals import JournalManager, JournalChange, merge_journals
from connectors.codex_bridge.search import SearchRequest, SearchService
from connectors.codex_bridge.server import LOCAL_ORIGIN, PUBLIC_ORIGIN, create_app
from src.custom_journals import clean_journals, normalize_issn, journal_groups, load_custom_journals, PUBLIC_PATH
from src.manual_search import fetch_source
from src.sources.public_literature import CrossrefAdapter
from src.models import SourceStatus

NATURE = {'issn': '2041-1723', 'name': 'Nature Communications', 'group': 'CNS 子刊', 'enabled': True}
SCIENCE = {'issn': '2375-2548', 'name': 'Science Advances', 'group': 'CNS 子刊', 'enabled': True}


def setup(root, rows=()):
    path = root / PUBLIC_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'journals': clean_journals(list(rows))}), encoding='utf-8')
    (root / 'config/venues.yml').write_text('groups:\n  - id: CNS 子刊\n    platform: Nature Portfolio (CNS)\n    journals: [Nature Communications]\n', encoding='utf-8')
    manager = JournalManager(root)
    for row in (NATURE, SCIENCE):
        manager.verified[row['issn']] = {**row, 'issns': [row['issn']]}
    return manager


def save(manager, row):
    return manager.save(JournalChange(revision=manager.snapshot()['revision'], **row))


def remote_file(rows):
    return {'sha': 'remote-sha', 'content': base64.b64encode(json.dumps({'journals': clean_journals(rows)}).encode()).decode()}


def test_issn_checksum_and_duplicate_print_electronic():
    assert normalize_issn(' 20411723 ') == '2041-1723'
    assert normalize_issn('0028-0836') == '0028-0836'
    assert normalize_issn('1050-124x') == '1050-124X'
    for value in ('2041-1724', '../config', '2041-1723?key=abc', '２０４１-１７２３'):
        with pytest.raises(ValueError): normalize_issn(value)
    with pytest.raises(ValueError):
        clean_journals([{**NATURE, 'issns': [SCIENCE['issn']]}, SCIENCE])
    with pytest.raises(ValueError): clean_journals([NATURE, {**SCIENCE, 'name': ' nature communications '}])
    assert 'api_key' not in clean_journals([{**NATURE, 'api_key': 'private'}])[0]


def test_private_persistence_edit_disable_remove_and_default_catalog(tmp_path):
    manager = setup(tmp_path)
    initial = (tmp_path / PUBLIC_PATH).read_bytes()
    saved = save(manager, NATURE)
    assert saved['pending'] and len(saved['journals']) == 1
    assert JournalManager(tmp_path).snapshot() == saved
    assert (tmp_path / PUBLIC_PATH).read_bytes() == initial  # GUI never edits the checkout.
    assert len(journal_groups(tmp_path)[0]['journals']) == 1  # No duplicate built-in.
    save(manager, {**NATURE, 'name': '自然·通讯', 'enabled': False})
    assert not load_custom_journals(tmp_path)[0]['enabled']
    assert load_custom_journals(tmp_path)[0]['canonical_name'] == NATURE['name']
    assert len(journal_groups(tmp_path)[0]['journals']) == 1
    manager.remove(NATURE['issn'], manager.snapshot()['revision'])
    assert not manager.snapshot()['journals']
    assert journal_groups(tmp_path)[0]['journals'] == [NATURE['name']]


def test_validation_failure_and_stale_edits_do_not_lose_saved_data(tmp_path):
    manager = setup(tmp_path)
    old = manager.snapshot()['revision']
    saved = save(manager, NATURE)
    with pytest.raises(HTTPException) as exc:
        manager.save(JournalChange(revision=old, **SCIENCE))
    assert exc.value.status_code == 409 and manager.snapshot() == saved
    manager.lookup = lambda _: (_ for _ in ()).throw(HTTPException(503, 'offline'))
    with pytest.raises(HTTPException): save(manager, SCIENCE)
    assert manager.snapshot() == saved


def test_three_way_sync_preserves_remote_additions_and_only_writes_public_fields(tmp_path):
    manager = setup(tmp_path)
    save(manager, NATURE)
    calls = []
    def remote(method, payload=None):
        calls.append((method, payload))
        if method == 'GET': return remote_file([SCIENCE])
        return {'commit': {'html_url': 'https://github.com/tengda-xmu/daily-papers/commit/abc123'}}
    manager.remote = remote
    result = manager.sync(manager.snapshot()['revision'])
    assert not result['pending'] and len(result['journals']) == 2
    payload = calls[1][1]
    assert payload['sha'] == 'remote-sha' and payload['branch'] == 'main'
    assert set(json.loads(base64.b64decode(payload['content']))) == {'journals'}
    assert all(set(r) == {'issn', 'issns', 'name', 'canonical_name', 'group', 'enabled'} for r in result['journals'])
    manager.remote = lambda method, body=None: remote_file(result['journals']) if method == 'GET' else pytest.fail('Unexpected PUT')
    manager.sync(result['revision'])  # Idempotent, no second commit.


def test_sync_conflicts_and_failures_keep_local_changes(tmp_path):
    base = clean_journals([NATURE])
    local = clean_journals([{**NATURE, 'group': '研究关注'}])
    remote = clean_journals([{**NATURE, 'enabled': False}])
    with pytest.raises(HTTPException) as exc: merge_journals(base, local, remote)
    assert exc.value.status_code == 409
    assert merge_journals(base, [], base) == []
    manager = setup(tmp_path)
    saved = save(manager, NATURE)
    manager.remote = lambda *args: (_ for _ in ()).throw(HTTPException(503, 'offline'))
    with pytest.raises(HTTPException): manager.sync(saved['revision'])
    assert manager.snapshot() == saved


def test_canonical_name_keeps_classification_after_display_rename(tmp_path, monkeypatch):
    from src import catalog
    row = clean_journals([{**NATURE, 'name': '自然·通讯', 'canonical_name': NATURE['name']}])[0]
    monkeypatch.setattr(catalog, 'JOURNALS', [{**row, 'platform': 'Crossref'}])
    assert catalog.paper_facets({'venue': NATURE['name']})['journal'] == '自然·通讯'


def test_journal_routes_require_pairing_and_trusted_origin(tmp_path):
    setup(tmp_path)
    app = create_app(tmp_path)
    app.state.journals.verified[NATURE['issn']] = {**NATURE, 'issns': [NATURE['issn']]}
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert client.get('/api/journals').status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}).json()['token']
        headers = {'Authorization': 'Bearer ' + token, 'Origin': PUBLIC_ORIGIN}
        assert client.get('/api/journals', headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        data = client.get('/api/journals', headers=headers).json()
        response = client.post('/api/journals', headers=headers, json={**NATURE, 'revision': data['revision']})
        assert response.status_code == 200
        assert client.get('/api/search/sources', headers=headers).json()['journals'][0]['issn'] == NATURE['issn']
        assert client.get('/api/journals/lookup/2041-1724', headers=headers).status_code == 400
        saved = response.json()
        assert client.delete('/api/journals/' + NATURE['issn'], headers={'Origin': PUBLIC_ORIGIN}).status_code == 401
        assert client.request('DELETE', '/api/journals/' + NATURE['issn'], headers=headers, json={'revision': saved['revision']}).status_code == 200


def test_daily_journal_queries_issn_date_filter_and_disabled_skip(monkeypatch):
    calls = []
    adapter = CrossrefAdapter(queries=['agents'], custom_journals=[NATURE, {**SCIENCE, 'enabled': False}])
    def general(since, until):
        adapter._status = SourceStatus('Crossref', 'no_data')
        return []
    monkeypatch.setattr(adapter, '_fetch_general', general)
    monkeypatch.setattr('src.sources.public_literature.time.sleep', lambda _: None)
    def get(url):
        calls.append(parse_qs(urlsplit(url).query))
        return {'message': {'items': [{'title': ['AI agents for structural design'], 'DOI': '10.1234/abc', 'published': {'date-parts': [[2025, 3, 1]]}}]}}
    monkeypatch.setattr(adapter, '_get_json', get)
    rows = adapter.fetch(datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, tzinfo=timezone.utc))
    assert len(rows) == 1 and len(calls) == 1
    assert calls[0]['filter'] == ['from-pub-date:2025-01-01,until-pub-date:2025-12-31,issn:2041-1723']
    assert rows[0].raw_metadata['subscribed_journal'] == NATURE['name']
    assert adapter.status.status == 'ok'
    assert CrossrefAdapter(queries=['manual']).custom_journals == []


def test_failed_journal_does_not_abort_other_journals(monkeypatch):
    adapter = CrossrefAdapter(queries=['agents'], custom_journals=[NATURE, SCIENCE])
    def general(*_):
        adapter._status = SourceStatus('Crossref', 'no_data'); return []
    monkeypatch.setattr(adapter, '_fetch_general', general)
    monkeypatch.setattr('src.sources.public_literature.time.sleep', lambda _: None)
    calls = []
    def get(url):
        calls.append(url)
        if '2041-1723' in url: raise TimeoutError()
        return {'message': {'items': [{'title': ['Agent test'], 'DOI': '10.1234/xyz', 'published': {'date-parts': [[2025, 3, 1]]}}]}}
    monkeypatch.setattr(adapter, '_get_json', get)
    assert len(adapter.fetch(datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, tzinfo=timezone.utc))) == 1
    assert len(calls) == 2 and adapter.status.status == 'partial'


def test_manual_scope_is_real_issn_filter_and_can_browse_without_keywords(tmp_path, monkeypatch):
    request = SearchRequest(query='', journal_issn='20411723', sources=['Crossref', 'Google Scholar'], since='2025-01-01', until=date.today())
    assert request.sources == ['Crossref'] and request.journal_issn == '2041-1723'
    with pytest.raises(ValueError): SearchRequest(query='', sources=['Crossref'], since='2025-01-01', until=date.today())
    urls = []
    monkeypatch.setattr(CrossrefAdapter, '_get_json', lambda self, url: urls.append(url) or {'message': {'items': []}})
    fetch_source('Crossref', '', datetime(2025, 1, 1), datetime.now(), 10, tmp_path, journal_issn=request.journal_issn)
    params = parse_qs(urlsplit(urls[0]).query)
    assert 'issn:2041-1723' in params['filter'][0]
    assert params['sort'] == ['published'] and 'query.bibliographic' not in params


def test_manual_journal_cache_does_not_mix_different_journals(tmp_path):
    async def run():
        calls = []
        async def worker(source, request):
            calls.append(request.journal_issn); return {'state': 'no_data', 'records': []}
        service = SearchService(tmp_path, tmp_path, worker)
        for journal in (NATURE, SCIENCE, NATURE):
            request = SearchRequest(query='', journal_issn=journal['issn'], sources=['Crossref'], since='2025-01-01', until=date.today())
            identifier = service.start(request); await service.get(identifier)['task']
        assert calls == [NATURE['issn'], SCIENCE['issn']]
        await service.close()
    asyncio.run(run())


def test_daily_quota_stops_following_requests(monkeypatch):
    adapter = CrossrefAdapter(queries=['agents'], custom_journals=[NATURE, SCIENCE])
    def general(*_):
        adapter._status = SourceStatus('Crossref', 'no_data'); return []
    monkeypatch.setattr(adapter, '_fetch_general', general)
    monkeypatch.setattr('src.sources.public_literature.time.sleep', lambda _: None)
    calls = []
    def get(url):
        calls.append(url); raise HTTPError(url, 429, 'Limited', {}, None)
    monkeypatch.setattr(adapter, '_get_json', get)
    assert not adapter.fetch(datetime(2025, 1, 1), datetime(2025, 12, 31))
    assert len(calls) == 1 and adapter.status.status == 'quota_exhausted'


def test_already_verified_journal_can_be_paused_offline_after_restart(tmp_path):
    manager = setup(tmp_path)
    save(manager, NATURE)
    manager = JournalManager(tmp_path)
    manager.lookup = lambda _: pytest.fail('Already verified journal should not need network for editing')
    assert not save(manager, {**NATURE, 'enabled': False})['journals'][0]['enabled']


def test_lookup_retries_transient_transport_once_but_never_quota(tmp_path, monkeypatch):
    manager = JournalManager(tmp_path)
    calls = []
    def transport(request, **kwargs):
        calls.append(request.full_url)
        if len(calls) == 1: raise URLError('TLS EOF')
        return BytesIO(json.dumps({'message': {'title': NATURE['name'], 'ISSN': [NATURE['issn']]}}).encode())
    monkeypatch.setattr('connectors.codex_bridge.journals.urlopen', transport)
    monkeypatch.setattr('connectors.codex_bridge.journals.time.sleep', lambda _: None)
    assert manager.lookup(NATURE['issn'])['name'] == NATURE['name']
    manager.lookup(NATURE['issn'])
    assert len(calls) == 2
    def quota(request, **kwargs):
        calls.append(request.full_url); raise HTTPError(request.full_url, 429, '', {}, None)
    monkeypatch.setattr('connectors.codex_bridge.journals.urlopen', quota)
    with pytest.raises(HTTPException): manager.lookup(SCIENCE['issn'])
    assert len(calls) == 3
