import asyncio
import base64
from copy import deepcopy
import json
from pathlib import Path
import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.editions import (History, append, candidates, delete_editions, entries, manifest,
    migrate, read, relative_path, revision, write)
from src.pipeline import run_pipeline
from tests.test_recommendation_editions import NOW, paper, payload
from tests.test_codex_bridge import FakeCodex
from connectors.codex_bridge.edition_manager import EditionManager, DeleteRequest
from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN


def seed(data):
    batches = [append(data, payload(1, [paper(1)], [paper(2)])),
               append(data, payload(2, [paper(3)], [paper(4)])),
               append(data, payload(3, [paper(5)], [paper(6)]))]
    write(data / 'daily.json', batches[-1])
    return batches


def remove(data, ids, identifier=None):
    return delete_editions(data, ids, revision(manifest(data)), identifier or str(uuid.uuid4()))


def test_delete_revokes_history_preserves_candidates_numbers_and_current(tmp_path):
    batches = seed(tmp_path)
    before = (tmp_path / 'daily.json').read_bytes()
    result = remove(tmp_path, ['1', '2'])
    assert [e['number'] for e in entries(tmp_path)] == [3]
    assert len(result['deleted']) == 2
    assert not (tmp_path / relative_path(batches[0]['edition'])).exists()
    assert History(tmp_path).find(paper(1)) is None
    assert History(tmp_path).find(paper(5))['last_core']['id'] == '3'
    assert set(candidates(tmp_path)) == {paper(i)['id'] for i in range(1, 5)}
    assert (tmp_path / 'daily.json').read_bytes() == before
    migrate(tmp_path, batches)
    assert len(entries(tmp_path)) == 1
    next_batch = append(tmp_path, payload(4, [paper(8)]))
    assert next_batch['edition']['number'] == 4


def test_deleted_papers_can_be_selected_again_without_rediscovery(tmp_path, monkeypatch):
    seed(tmp_path)
    remove(tmp_path, ['1'])
    monkeypatch.setenv('GITHUB_RUN_ID', 'new-after-deletion')
    result = run_pipeline(until=NOW, adapters=[], output_path=tmp_path / 'daily.json')
    assert {p['id'] for p in result['core'] + result['extended']} == {paper(1)['id'], paper(2)['id']}
    assert all(not p.get('recommendation_decision') for p in result['core'] + result['extended'])


def test_other_appearances_and_aliases_still_apply(tmp_path):
    seed(tmp_path)
    repeated = {**paper(1), 'title': 'Updated fault diagnosis title'}
    b = append(tmp_path, payload(4, [repeated]))
    write(tmp_path / 'daily.json', b)
    remove(tmp_path, ['1'])
    h = History(tmp_path)
    assert h.find(paper(1))['last_core']['id'] == '4'
    variant = {**paper(2), 'id': 'ffffffffffff', 'source': 'Another source'}
    assert h.identity(variant) == paper(2)['id'] and h.find(variant) is None


def test_validation_atomic_idempotency_and_recovery(tmp_path):
    seed(tmp_path)
    version = revision(manifest(tmp_path))
    identifier = str(uuid.uuid4())
    for ids, rev in [(['1', '3'], version), (['1', 'missing'], version), (['1'], 'stale')]:
        with pytest.raises(ValueError):
            delete_editions(tmp_path, ids, rev, identifier)
        assert len(entries(tmp_path)) == 3 and candidates(tmp_path) == {}
    result = delete_editions(tmp_path, ['1'], version, identifier)
    assert delete_editions(tmp_path, ['1'], version, identifier) == result
    with pytest.raises(ValueError):
        delete_editions(tmp_path, ['2'], version, identifier)
    assert len(entries(tmp_path)) == 2


def test_reconcile_deleted_remote_or_local_never_resurrects(tmp_path):
    from tools.recommendation_data import reconcile
    data = tmp_path / 'data'; batches = seed(data)
    old = {'editions/index.json': deepcopy(manifest(data)), **{relative_path(b['edition']): b for b in batches}}
    remove(data, ['1'])
    reconcile(data, old.__getitem__)
    assert [e['id'] for e in entries(data)] == ['2', '3']
    remote = {'editions/index.json': manifest(data), **{relative_path(b['edition']): b for b in batches}}
    fresh = tmp_path / 'fresh'
    reconcile(fresh, remote.__getitem__)
    assert [e['number'] for e in entries(fresh)] == [2, 3]
    assert read(fresh / 'daily.json')['edition']['id'] == '3'


def test_build_removes_old_files_and_dead_provenance_links(tmp_path, monkeypatch):
    from tools import build_site
    data, out = tmp_path / 'data', tmp_path / 'site'
    batches = seed(data)
    monkeypatch.setattr(build_site, 'DATA', data); monkeypatch.setattr(build_site, 'OUT', out)
    build_site.build_archive()
    target = out / 'archive/2026-09-25--1.html'
    assert target.exists()
    remove(data, ['1'])
    build_site.build_archive()
    assert not target.exists() and not target.with_suffix('.json').exists()
    html = (out / 'archive/index.html').read_text(encoding='utf-8')
    assert '2 批归档' in html and '当前推荐' in html and 'archive-manage' in html
    p = paper(1); p['recommendation_decision'] = {'kind': 'promotion', 'previous': batches[0]['edition'], 'reason': 'Earlier decision'}
    assert '该批次已删除' in build_site.recommendation_context(p, '../')
    assert '2026-09-25--1.html' not in build_site.recommendation_context(p, '../')


class Remote:
    def __init__(self, value):
        self.value = value; self.calls = []; self.lost = False
        self.status, self.conclusion = 'in_progress', None

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if '/contents/' in path:
            return {'content': base64.b64encode(json.dumps(self.value).encode()).decode()}
        if method == 'POST':
            self.identifier = body['inputs']['request_id']
            if self.lost:
                raise HTTPException(503, 'Lost response')
            return {'workflow_run_id': 42}
        if '/runs?' in path:
            return {'workflow_runs': [{'id': 42, 'display_title': 'Delete editions ' + self.identifier}]}
        if '/runs/' in path:
            return {'status': self.status, 'conclusion': self.conclusion}
        return {}


def setup_manager(tmp_path):
    seed(tmp_path / 'data')
    remote = Remote(manifest(tmp_path / 'data'))
    manager = EditionManager(tmp_path, tmp_path / 'runtime', remote, lambda _: remote.value)
    request = DeleteRequest(request_id=uuid.uuid4(), revision=revision(remote.value), edition_ids=['1'])
    return manager, remote, request


def test_manager_auth_stale_current_and_no_real_deletion(tmp_path):
    app = create_app(tmp_path, rpc=FakeCodex())
    manager, remote, request = setup_manager(tmp_path)
    app.state.edition_manager.remote = remote; app.state.edition_manager.public = lambda _: remote.value
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        body = request.model_dump(mode='json')
        assert client.post('/api/recommendations/editions/delete', json=body).status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}, headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        assert client.post('/api/recommendations/editions/delete', json=body, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/recommendations/editions/delete', json={**body, 'edition_ids': ['3']}, headers=headers).status_code == 409
        assert client.post('/api/recommendations/editions/delete', json={**body, 'workflow': 'evil.yml'}, headers=headers).status_code == 422
        assert client.post('/api/recommendations/editions/delete', json=body, headers=headers).json()['state'] == 'queued'
        assert len(entries(tmp_path / 'data')) == 3  # dispatch does not mutate the checkout


def test_manager_lost_response_restart_and_publication_retry(tmp_path):
    manager, remote, request = setup_manager(tmp_path)
    remote.lost = True
    assert manager.submit(request)['state'] == 'confirming'
    assert manager.submit(request)['state'] == 'confirming'
    restarted = EditionManager(tmp_path, tmp_path / 'runtime', remote, lambda _: remote.value)
    assert restarted.status(request.request_id)['run_id'] == '42'
    assert len([c for c in remote.calls if c[0] == 'POST']) == 1
    remote.status, remote.conclusion = 'completed', 'success'
    assert restarted.status(request.request_id, True)['state'] == 'publishing'
    remote.conclusion = 'failure'
    assert restarted.status(request.request_id, True)['state'] == 'failed'
    remote.value = delete_editions(tmp_path / 'data', ['1'], request.revision, str(request.request_id))
    # Even a failed workflow can have deployed successfully; verify the marker.
    assert restarted.retry(request.request_id)['state'] == 'succeeded'
    assert len([c for c in remote.calls if c[0] == 'POST']) == 1


def test_local_archive_ignores_deleted_cache_but_paper_still_resolves(tmp_path):
    app = create_app(tmp_path, rpc=FakeCodex())
    batches = seed(tmp_path / 'data')
    runtime = app.state.store.runtime
    write(runtime / 'public-editions/2026-09-25/1.json', batches[0])
    value = remove(tmp_path / 'data', ['1'])
    write(runtime / 'edition-manifest.json', value)
    before = app.state.store.paper(paper(1)['id'])
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert '2026-09-25--1.html' not in client.get('/archive/').text
        assert client.get('/archive/2026-09-25--1.html').status_code == 404
    assert before['id'] == paper(1)['id']
    (runtime / 'public-editions/2026-09-25/1.json').unlink()
    assert app.state.store.paper(paper(1)['id'])['id'] == paper(1)['id']


def test_local_archive_management_script_is_served(tmp_path):
    app = create_app(Path(__file__).resolve().parents[1], runtime=tmp_path, rpc=FakeCodex())
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        response = client.get('/assets/archive-manager.js')
        assert response.status_code == 200 and 'archive-manage' in response.text


def test_queue_disables_revoked_only_and_retains_results(tmp_path):
    from connectors.codex_bridge.reading_queue import ReadingQueue
    data = tmp_path / 'data'; batches = seed(data)
    public = {'editions/index.json': manifest(data), 'data.json': batches[-1],
              **{relative_path(b['edition']): b for b in batches}}
    queue = ReadingQueue(tmp_path, tmp_path / 'runtime', None, asyncio.Lock(), fetch=public.__getitem__)
    queue.sync()
    with queue.db() as db:
        db.execute("UPDATE tasks SET result='kept-result' WHERE paper_id=?", (paper(1)['id'],))
    public['editions/index.json'] = remove(data, ['1'])
    queue.sync()
    with queue.db() as db:
        deleted = db.execute('SELECT * FROM tasks WHERE paper_id=?', (paper(1)['id'],)).fetchone()
        assert deleted['enabled'] == 0 and deleted['result'] == 'kept-result'
        assert db.execute('SELECT enabled FROM tasks WHERE paper_id=?', (paper(5)['id'],)).fetchone()[0] == 1
    assert read(tmp_path / 'runtime/public-editions/2026-09-25/1.json')
