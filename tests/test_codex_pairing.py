"""Remembered browsers must survive restarts without opening unauthenticated access."""
import hashlib
import json
import time

from fastapi.testclient import TestClient
import pytest

from connectors.codex_bridge.server import create_app, LOCAL_ORIGIN, PUBLIC_ORIGIN


def headers(token, origin=PUBLIC_ORIGIN):
    return {'Authorization': 'Bearer ' + token, 'Origin': origin}


def pair(client, app, remember=True, origin=PUBLIC_ORIGIN):
    response = client.post('/api/pair', json={'code': app.state.pair_code, 'remember': remember},
                           headers={'Origin': origin})
    assert response.status_code == 200
    return response.json()


def restore(client, credential, origin=PUBLIC_ORIGIN):
    return client.post('/api/session/restore', json={'device_token': credential}, headers={'Origin': origin})


def test_browser_authorization_survives_restart_and_stores_only_hash(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        data = pair(client, app)
        old_code = app.state.pair_code
        credential = data['device_token']
        assert credential != data['token']
        assert data['expires_at'] < data['device_expires_at']
        with app.state.store.connect() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM trusted_browsers')]
        assert rows[0]['token_hash'] == hashlib.sha256(credential.encode()).hexdigest()
        assert rows[0]['origin'] == PUBLIC_ORIGIN
        assert credential not in json.dumps(rows)
        assert credential.encode() not in app.state.store.db.read_bytes()
        # A browser credential is not an access token or the startup pairing code.
        assert client.get('/api/papers', headers=headers(credential)).status_code == 401
        assert client.post('/api/pair', json={'code': credential}).status_code == 403

    restarted = create_app(tmp_path)
    assert restarted.state.pair_code != old_code
    with TestClient(restarted, base_url=LOCAL_ORIGIN) as client:
        assert client.get('/api/papers', headers=headers(data['token'])).status_code == 401
        response = restore(client, credential)
        assert response.status_code == 200
        new = response.json()
        assert new['token'] != data['token'] and 'device_token' not in new
        assert client.get('/api/papers', headers=headers(new['token'])).status_code == 200
        assert new['device_expires_at'] >= data['device_expires_at']


@pytest.mark.parametrize('origin,status', [(LOCAL_ORIGIN, 401), ('https://untrusted.example', 403), (None, 403)])
def test_remembered_authorization_is_bound_to_origin(tmp_path, origin, status):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        data = pair(client, app)
        extra = {'Origin': origin} if origin else {}
        payload = {'device_token': data['device_token']}
        assert client.post('/api/session/restore', json=payload, headers=extra).status_code == status
        client.post('/api/session/forget', json=payload, headers=extra)
        # A different origin can neither restore nor revoke the original grant.
        assert restore(client, data['device_token']).status_code == 200


def test_remember_is_opt_in_and_existing_sessions_can_upgrade(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        data = pair(client, app, remember=False)
        assert 'device_token' not in data
        with app.state.store.connect() as db:
            assert db.execute('SELECT COUNT(*) FROM trusted_browsers').fetchone()[0] == 0
        assert client.post('/api/session/remember', headers={'Origin': PUBLIC_ORIGIN}).status_code == 401
        assert client.post('/api/session/remember', headers={'Authorization': 'Bearer ' + data['token']}).status_code == 403
        response = client.post('/api/session/remember', headers=headers(data['token']))
        assert response.status_code == 200
        assert restore(client, response.json()['device_token']).status_code == 200


def test_expired_or_forged_browser_cannot_restore_or_gain_access(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        data = pair(client, app)
        with app.state.store.connect() as db:
            db.execute('UPDATE trusted_browsers SET expires=?', (time.time() - 1,))
        for credential in (data['device_token'], 'A' * 43, 'not-a-token'):
            response = restore(client, credential)
            assert response.status_code == 401
            assert response.json()['state'] == 'unpaired'
            assert 'token' not in response.json()
        assert client.get('/api/papers').status_code == 401


def test_forgetting_revokes_all_browser_sessions_and_survives_restart(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        data = pair(client, app)
        other = pair(client, app, origin=LOCAL_ORIGIN)
        tokens = [data['token'], restore(client, data['device_token']).json()['token'],
                  restore(client, data['device_token']).json()['token']]
        response = client.post('/api/session/forget', json={'device_token': data['device_token']},
                               headers={'Origin': PUBLIC_ORIGIN})
        assert response.status_code == 200
        for token in tokens:
            assert client.get('/api/papers', headers=headers(token)).status_code == 401
        assert restore(client, data['device_token']).status_code == 401
        assert restore(client, other['device_token'], LOCAL_ORIGIN).status_code == 200
    with TestClient(create_app(tmp_path), base_url=LOCAL_ORIGIN) as client:
        assert restore(client, data['device_token']).status_code == 401


def test_unauthorized_restore_is_rate_limited_and_pairing_is_validated(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app, base_url=LOCAL_ORIGIN) as client:
        assert client.post('/api/pair', json={'code': '中文无效码'}).status_code == 403
        assert client.post('/api/pair', json=['wrong shape']).status_code == 422
        assert client.post('/api/pair', json={'code': 'x' * 2000}).status_code == 413
        for _ in range(7):
            assert restore(client, 'A' * 43).status_code == 401
        assert restore(client, 'A' * 43).status_code == 429
