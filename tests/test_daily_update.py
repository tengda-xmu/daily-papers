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


def test_no_new_check_completes_without_overwriting_edition_history(tmp_path):
    remote = Remote()
    prior = result('122')
    prior['_update_status'] = {'run_id': '123', 'outcome': 'no_new'}
    updater = DailyUpdater(tmp_path, remote, fetch=lambda run: dict(prior))
    updater.start(uuid.uuid4())
    remote.status, remote.conclusion = 'completed', 'success'
    data = updater.snapshot()
    assert data['state'] == 'succeeded' and data['outcome'] == 'no_new'
    assert data['changed'] is False and not (tmp_path / 'recommendation-history/123.json').exists()


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
        assert client.post('/api/recommendations/catch-up', json={}).status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}, headers={'Origin': PUBLIC_ORIGIN}).json()['token']
        headers = {'Origin': PUBLIC_ORIGIN, 'Authorization': 'Bearer ' + token}
        assert client.post('/api/recommendations/update', json=request, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        assert client.post('/api/recommendations/update', json={**request, 'workflow': 'evil.yml'}, headers=headers).status_code == 422
        assert remote.calls == []
        assert client.post('/api/recommendations/catch-up', json={'workflow':'evil'}, headers=headers).status_code == 422
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


def test_columns_only_receipt_completes_without_new_edition(tmp_path):
    remote=Remote()
    prior=result('122')
    prior['_update_status']={'run_id':'123','outcome':'columns_only','checked_at':'2026-09-30T03:00:00Z'}
    updater=DailyUpdater(tmp_path,remote,fetch=lambda _:dict(prior))
    updater.start(uuid.uuid4())
    remote.status,remote.conclusion='completed','success'
    data=updater.snapshot()
    assert data['state']=='succeeded' and data['outcome']=='columns_only'
    assert not (tmp_path/'recommendation-history/123.json').exists()


def test_publish_failure_retries_existing_artifact_without_new_dispatch(tmp_path):
    from tests.test_daily_schedule import Remote as ScheduledRemote, NOW, edition
    from datetime import timedelta
    remote=ScheduledRemote(edition('2026-09-27T21:00:00Z'))
    calls=[]
    def remote_with_jobs(method,path,body=None):
        calls.append((method,path,body))
        if '/jobs?' in path:return {'jobs':[{'name':'update','conclusion':'success'},{'name':'publish','conclusion':'failure'}]}
        return remote(method,path,body)
    updater=DailyUpdater(tmp_path,remote_with_jobs,fetch=lambda _:result('122'))
    updater.catch_up(now=NOW)
    remote.status,remote.conclusion='completed','failure'
    updater.last_check=0
    assert updater.catch_up(now=NOW+timedelta(minutes=1))['state']=='failed'
    assert updater.catch_up(now=NOW+timedelta(minutes=2))['state']=='failed'
    assert updater.catch_up(now=NOW+timedelta(minutes=7))['state']=='queued'
    posts=[p for method,p,_ in calls if method=='POST']
    assert len(posts)==2 and posts[-1].endswith('/runs/123/rerun-failed-jobs')


def test_background_monitor_checks_immediately_and_stops(tmp_path):
    import asyncio
    async def run():
        updater=DailyUpdater(tmp_path)
        checked=asyncio.Event()
        loop=asyncio.get_running_loop()
        updater.catch_up=lambda:loop.call_soon_threadsafe(checked.set)
        updater.start_monitor()
        await asyncio.wait_for(checked.wait(),timeout=3)
        await updater.close()
        assert updater.background.done()
    asyncio.run(run())


def test_isolated_app_does_not_dispatch_live_workflows(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(DailyUpdater,'start_monitor',lambda _:calls.append(True))
    with TestClient(create_app(tmp_path),base_url=LOCAL_ORIGIN) as client:
        assert client.get('/api/health').status_code==200
    assert calls==[]


def test_publication_snapshot_removes_old_pages_and_preserves_private_data(tmp_path):
    import json
    from tools.restore_publication import restore
    snap=tmp_path/'.publication'
    for name,body in [('data/daily.json',{}),('data/editions/index.json',{'deleted':[{'id':'old'}]}),('site/data.json',{})]:
        path=snap/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(body))
    (snap/'site/index.html').write_text('new')
    (tmp_path/'site/archive').mkdir(parents=True)
    (tmp_path/'site/archive/deleted.html').write_text('old')
    (tmp_path/'.local').mkdir();private=tmp_path/'.local/private.pdf';private.write_bytes(b'private')
    restore(snap,tmp_path)
    assert (tmp_path/'site/index.html').read_text()=='new'
    assert not (tmp_path/'site/archive/deleted.html').exists()
    assert private.read_bytes()==b'private'
    (snap/'site/data.json').unlink()
    with __import__('pytest').raises(ValueError):restore(snap,tmp_path)
    assert (tmp_path/'site/index.html').read_text()=='new'


def test_automatic_gate_skipped_after_another_run_does_not_wait_forever(tmp_path, monkeypatch):
    from tests.test_daily_schedule import Remote as ScheduledRemote, NOW, edition
    from datetime import datetime
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz)
    # The scenario and its background snapshot must use the same clock, even
    # when this regression test runs days after its fixed fixture dates.
    monkeypatch.setattr('tools.daily_schedule.datetime', Clock)
    remote=ScheduledRemote(edition('2026-09-27T21:00:00Z'))
    def with_jobs(method,path,body=None):
        if '/jobs?' in path:return {'jobs':[{'name':'update','conclusion':'skipped'}]}
        return remote(method,path,body)
    current={**edition(),'update_run_id':'124'}
    updater=DailyUpdater(tmp_path,with_jobs,fetch=lambda _:dict(current))
    updater.catch_up(now=NOW)
    remote.status,remote.conclusion='completed','success'
    remote.files['daily.json']=current
    remote.files['update-status.json']={'run_id':'124','outcome':'published','checked_at':current['generated_at']}
    assert updater.snapshot()['state']=='current'
    assert updater.snapshot()['run_id']=='124'
