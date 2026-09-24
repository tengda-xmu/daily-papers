import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from fastapi.testclient import TestClient

from connectors.codex_bridge.daily_update import API, DailyUpdater
from connectors.codex_bridge.server import LOCAL_ORIGIN, PUBLIC_ORIGIN, create_app
from tests.test_codex_bridge import FakeCodex
from tools.build_site import render


class Remote:
    def __init__(self):
        self.calls = []
        self.status, self.conclusion = 'in_progress', None
        self.lost = False

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'POST':
            self.request_id = body['inputs']['request_id']
            if self.lost:
                raise HTTPException(503, 'network interrupted')
            return {'workflow_run_id': 123}
        if '/runs?' in path:
            return {'workflow_runs': [{'id': 123, 'display_title': 'Manual papers ' + self.request_id}]}
        if '/jobs?' in path:
            return {'jobs': [{'steps': [{'name': 'Run daily pipeline', 'status': 'in_progress'}]}]}
        if '/runs/' in path:
            return {'status': self.status, 'conclusion': self.conclusion}
        return {'state': 'active'}


def result(run_id='123'):
    return {'update_run_id': run_id, 'generated_at': '2026-09-24T08:00:00Z',
            'core': [{'id': '111111111111', 'title': 'New core paper'}],
            'extended': [{'id': '222222222222', 'title': 'New extended paper'}]}


def test_update_deduplicates_concurrent_tabs_and_survives_restart(tmp_path):
    remote = Remote()
    updater = DailyUpdater(tmp_path, remote)
    with ThreadPoolExecutor(3) as pool:
        jobs = list(pool.map(updater.start, [uuid.uuid4() for _ in range(3)]))
    assert {job['run_id'] for job in jobs} == {'123'}
    posts = [call for call in remote.calls if call[0] == 'POST']
    assert len(posts) == 1
    assert posts[0][1] == API + '/workflows/daily.yml/dispatches'
    assert posts[0][2]['inputs']['send_digest'] is False
    restarted = DailyUpdater(tmp_path, remote)
    assert restarted.snapshot()['state'] == 'running'
    restarted.start(uuid.uuid4())
    assert len([c for c in remote.calls if c[0] == 'POST']) == 1


def test_lost_dispatch_response_is_resolved_without_second_dispatch(tmp_path):
    remote = Remote(); remote.lost = True
    updater = DailyUpdater(tmp_path, remote)
    assert updater.start(uuid.uuid4())['state'] == 'confirming'
    assert updater.snapshot()['run_id'] == '123'
    assert len([c for c in remote.calls if c[0] == 'POST']) == 1


def test_stale_publication_never_counts_as_success_and_failure_preserves_data(tmp_path):
    remote = Remote()
    updater = DailyUpdater(tmp_path, remote, lambda _: result('122'))
    initial = updater.start(uuid.uuid4())
    remote.status, remote.conclusion = 'completed', 'success'
    assert updater.snapshot()['state'] == 'publishing'
    assert not (tmp_path / 'recommendations.json').exists()
    updater.fetch = lambda _: result()
    updater.last_check = 0
    assert updater.snapshot()['state'] == 'succeeded'
    before = (tmp_path / 'recommendations.json').read_bytes()
    assert updater.start(initial['request_id'])['state'] == 'succeeded'
    updater.start(uuid.uuid4())
    remote.conclusion = 'failure'
    updater.last_check = 0
    assert updater.snapshot()['state'] == 'failed'
    assert (tmp_path / 'recommendations.json').read_bytes() == before


def test_authentication_fixed_dispatch_and_new_papers_available_to_chat(tmp_path):
    app = create_app(tmp_path, rpc=FakeCodex())
    remote = Remote()
    app.state.daily_updater.remote = remote
    app.state.daily_updater.fetch = lambda _: result()
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        request = {'request_id': str(uuid.uuid4())}
        assert client.post('/api/recommendations/update', json=request).status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}, headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        assert client.post('/api/recommendations/update', json=request, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/recommendations/update', json={**request, 'workflow': 'evil.yml'}, headers=headers).status_code == 422
        assert remote.calls == []
        assert client.post('/api/recommendations/update', json=request, headers=headers).json()['state'] == 'queued'
        remote.status, remote.conclusion = 'completed', 'success'
        assert client.get('/api/recommendations/update', headers=headers).json()['state'] == 'succeeded'
        for paper in result()['core'] + result()['extended']:
            response = client.get('/api/papers/' + paper['id'], headers=headers)
            assert response.status_code == 200
            assert response.json()['paper']['title'] == paper['title']
        assert len(client.get('/api/papers', headers=headers).json()['papers']) == 2
        page = client.get('/recommendations.html').text
        assert 'New core paper' in page and 'New extended paper' in page
        assert 'data-run-id="123"' in page


def test_home_controls_are_compact_and_archives_remain_immutable():
    home = render(result())
    assert 'class="page-heading"' not in home
    assert '手动检索文献</a>' not in home
    assert 'id="manual-update"' in home
    assert 'id="daily-update-pair"' in home
    archive = render(result(), archive_date='2026-09-24')
    assert 'id="manual-update"' not in archive
    assert '返回最新一期' in archive
