import base64
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN
from connectors.codex_bridge.wechat_subscriptions import SubscriptionManager, SubscriptionChange, merge_accounts
from src.wechat_subscriptions import (LOCAL_PATH, PUBLIC_PATH, clean_accounts, effective_accounts,
    load_manual_accounts, manual_queries, published_accounts)
from src.sources import wechat_public_index as index


def setup(root):
    (root / 'config').mkdir(exist_ok=True)
    (root / PUBLIC_PATH).write_text('{"accounts": []}', encoding='utf-8')
    (root / 'config/wechat_accounts.json').write_text(json.dumps({'seed_names': ['已有公众号'],
        'public_article_queries': ['科研论文 {year}'], 'public_daily_queries': 3,
        'groups': [{'name': '可靠性与疲劳'}]}), encoding='utf-8')
    return SubscriptionManager(root)


def save(manager, **fields):
    return manager.save(SubscriptionChange(revision=manager.snapshot()['revision'], name='新研究号', **fields))


def remote_file(rows):
    return {'sha': 'sha', 'content': base64.b64encode(json.dumps({'accounts': rows}).encode()).decode()}


def test_crud_persists_with_duplicate_and_revision_guards(tmp_path):
    manager = setup(tmp_path)
    old = manager.snapshot()['revision']
    saved = save(manager, alias='research_lab', group='可靠性与疲劳')
    row = saved['accounts'][0]
    assert saved['pending'] and load_manual_accounts(tmp_path) == saved['accounts']
    assert SubscriptionManager(tmp_path).snapshot() == saved
    with pytest.raises(ValueError, match='重复'):
        save(manager, alias='other')
    with pytest.raises(ValueError, match='重复'):
        manager.save(SubscriptionChange(revision=saved['revision'], name='不同名称', alias='RESEARCH_LAB'))
    with pytest.raises(ValueError, match='现有订阅'):
        manager.save(SubscriptionChange(revision=saved['revision'], name='已有公众号'))
    with pytest.raises(HTTPException) as error:
        manager.remove(row['id'], old)
    assert error.value.status_code == 409 and manager.snapshot() == saved
    changed = manager.save(SubscriptionChange(revision=saved['revision'], **{**row, 'enabled': False}))
    assert changed['accounts'][0]['enabled'] is False
    assert not next(r for r in effective_accounts(tmp_path) if r['name'] == row['name'])['enabled']
    assert manager.remove(row['id'], changed['revision'])['accounts'] == []


def test_sync_merges_other_accounts_and_failure_keeps_local(tmp_path):
    manager = setup(tmp_path)
    state = save(manager, group='手动分组')
    row = state['accounts'][0]
    other = {**row, 'id': 'a' * 32, 'name': '另一研究号'}
    calls = []
    def remote(method, body=None):
        calls.append((method, body))
        if method == 'GET': return remote_file([other])
        assert body['sha'] == 'sha' and body['branch'] == 'main'
        payload = json.loads(base64.b64decode(body['content']))
        assert {r['name'] for r in payload['accounts']} == {'新研究号', '另一研究号'}
        assert all(set(r) == {'id', 'name', 'alias', 'group', 'enabled'} for r in payload['accounts'])
        return {'commit': {'html_url': 'https://github.com/tengda-xmu/daily-papers/commit/abc'}}
    manager.remote = remote
    synced = manager.sync(state['revision'])
    assert not synced['pending'] and len(synced['accounts']) == 2
    assert [c[0] for c in calls] == ['GET', 'PUT']
    manager.remote = lambda *args: (_ for _ in ()).throw(HTTPException(503, 'offline'))
    changed = manager.remove(row['id'], synced['revision'])
    with pytest.raises(HTTPException): manager.sync(changed['revision'])
    assert manager.snapshot() == changed
    with pytest.raises(HTTPException) as error:
        merge_accounts([row], [{**row, 'group': '本机修改'}], [{**row, 'group': '远程修改'}])
    assert error.value.status_code == 409


def test_directory_keeps_manual_accounts_when_discovery_snapshot_changes(tmp_path):
    manager = setup(tmp_path)
    row = save(manager)['accounts'][0]
    directory = tmp_path / 'data/inbox/wechat-subscriptions.json'
    directory.parent.mkdir(parents=True)
    directory.write_text(json.dumps({'accounts': [{'name': '自动发现号'}, {'name': row['name']}]}), encoding='utf-8')
    assert {r['name'] for r in effective_accounts(tmp_path)} == {'自动发现号', '新研究号'}
    assert sum(r['manual'] for r in effective_accounts(tmp_path)) == 1
    # The published site must not expose local edits until explicit sync.
    assert not published_accounts(tmp_path)
    assert not any(r['manual'] for r in effective_accounts(tmp_path, manual=published_accounts(tmp_path)))
    paused = manager.save(SubscriptionChange(revision=manager.snapshot()['revision'], **{**row, 'enabled': False}))
    assert len(paused['accounts']) == 1  # Discovery does not block later editing.
    assert not next(r for r in effective_accounts(tmp_path) if r['name'] == row['name'])['enabled']


def test_manual_names_participate_in_queries_and_filter_cached_results(tmp_path, monkeypatch):
    manager = setup(tmp_path)
    row = save(manager)['accounts'][0]
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(index, 'ROOT', tmp_path)
    monkeypatch.setattr(index.time, 'sleep', lambda _: None)
    calls = []
    def respond(request, **kwargs):
        calls.append(request.full_url)
        return BytesIO(('''<ul class="news-list"><li id="sogou_vr_1"><h3>可靠性论文</h3>
            <p class="txt-info">论文简介</p><span class="all-time-y2">新研究号</span>
            <script>timeConvert('%s')</script></li></ul>''' % int((now - timedelta(hours=1)).timestamp())).encode())
    monkeypatch.setattr(index, 'urlopen', respond)
    adapter = index.WeChatPublicIndexAdapter(cache_dir=tmp_path / 'cache')
    found = adapter.fetch(now - timedelta(days=30), now)
    assert len(found) == 1 and found[0].venue == '新研究号'
    from urllib.parse import parse_qs, urlsplit
    assert parse_qs(urlsplit(calls[0]).query)['query'][0].startswith('新研究号 ')
    assert len(calls) <= 3
    manager.save(SubscriptionChange(revision=manager.snapshot()['revision'], **{**row, 'enabled': False}))
    assert adapter.fetch(now - timedelta(days=30), now) == []
    assert len(calls) <= 3  # Cached rows are filtered against current subscriptions.
    assert manual_queries(effective_accounts(tmp_path), now) == []


def test_manual_rotation_reaches_all_accounts_within_budget():
    rows = [{'name': str(i), 'manual': True, 'enabled': True} for i in range(5)]
    now = datetime(2026, 9, 24, tzinfo=timezone.utc)
    seen = set()
    for day in range(5):
        queries = manual_queries(rows, now + timedelta(days=day))
        assert len(queries) == 2
        seen.update(q.split()[0] for q in queries)
    assert seen == {str(i) for i in range(5)}


def test_subscription_routes_auth_validation_and_fixed_sync_target(tmp_path, monkeypatch):
    setup(tmp_path)
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert client.get('/api/wechat-subscriptions').status_code == 401
        assert client.post('/api/wechat-subscriptions', json={}).status_code == 401
        token = client.post('/api/pair', json={'code': app.state.pair_code}).json()['token']
        headers = {'Authorization': 'Bearer ' + token, 'Origin': PUBLIC_ORIGIN}
        assert client.get('/api/wechat-subscriptions', headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        state = client.get('/api/wechat-subscriptions', headers=headers).json()
        payload = {'revision': state['revision'], 'name': '我的公众号'}
        assert client.post('/api/wechat-subscriptions', headers=headers, json={**payload, 'token': 'PRIVATE'}).status_code == 422
        assert client.post('/api/wechat-subscriptions', headers=headers, json={**payload, 'name': '<script>'}).status_code == 400
        added = client.post('/api/wechat-subscriptions', headers=headers, json=payload)
        assert added.status_code == 200
        state = added.json()
        assert len(state['accounts']) == 1
        app.state.wechat_subscriptions.remote = lambda method, body=None: remote_file([]) if method == 'GET' else {}
        synced = client.post('/api/wechat-subscriptions/sync', headers=headers, json={'revision': state['revision']})
        assert synced.status_code == 200 and not synced.json()['pending']
        removed = client.request('DELETE', '/api/wechat-subscriptions/' + state['accounts'][0]['id'],
            headers=headers, json={'revision': synced.json()['revision']})
        assert removed.status_code == 200 and removed.json()['accounts'] == []
    import connectors.codex_bridge.wechat_subscriptions as module
    seen = []
    monkeypatch.setattr(module, 'github', lambda method, body=None, **kwargs: seen.append(kwargs))
    module.remote_accounts('GET')
    assert seen == [{'endpoint': 'repos/tengda-xmu/daily-papers/contents/config/custom-wechat.json'}]
