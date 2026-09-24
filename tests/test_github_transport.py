import json
import subprocess
import uuid
from urllib.error import URLError

import pytest

from connectors.codex_bridge import daily_update as update


@pytest.fixture(autouse=True)
def network(monkeypatch):
    monkeypatch.setattr(update.os, 'environ', {})
    monkeypatch.setattr(update, '_prefer_system_proxy', False)
    monkeypatch.setattr(update, 'getproxies', lambda: {'https': 'http://proxy.example:8080'})


def test_failed_read_uses_alternate_route_and_dispatch_uses_verified_route(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            return subprocess.CompletedProcess(args, 1, b'', b'connection reset by peer')
        return subprocess.CompletedProcess(args, 0, b'{"state":"active"}', b'')

    monkeypatch.setattr(update.subprocess, 'run', run)
    assert update.github('GET', update.API + '/workflows/daily.yml')['state'] == 'active'
    update.github('POST', update.API + '/workflows/daily.yml/dispatches', {'ref': 'main'})
    assert len(calls) == 3
    assert 'HTTPS_PROXY' not in calls[0][1]['env']
    assert all(call[1]['env']['HTTPS_PROXY'] == 'http://proxy.example:8080' for call in calls[1:])
    assert json.loads(calls[-1][1]['input']) == {'ref': 'main'}


@pytest.mark.parametrize('explicit', [{'https_proxy': 'http://explicit.example:80'}, {'ALL_PROXY': 'socks5://explicit.example:80'}, {'HTTPS_PROXY': ''}])
def test_explicit_proxy_is_honored_on_retry(monkeypatch, explicit):
    monkeypatch.setattr(update.os, 'environ', explicit)
    monkeypatch.setattr(update, 'getproxies', lambda: pytest.fail('Must not replace explicit proxy settings'))
    calls = []

    def run(args, **kwargs):
        calls.append(kwargs['env'])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 10)
        return subprocess.CompletedProcess(args, 0, b'{}', b'')

    monkeypatch.setattr(update.subprocess, 'run', run)
    update.github('GET', update.API + '/workflows/daily.yml')
    assert len(calls) == 2
    assert all(all(env[key] == value for key, value in explicit.items()) for env in calls)


@pytest.mark.parametrize('error, expected', [
    (b'gh: Bad credentials (HTTP 401)', '登录'),
    (b'gh: Resource not accessible (HTTP 403)', '权限'),
    (b'gh: API rate limit exceeded (HTTP 403)', '受限'),
    (b'gh: Not Found (HTTP 404)', '不可访问'),
    (b'gh: Invalid workflow inputs (HTTP 422)', '配置'),
])
def test_definite_errors_are_specific_redacted_and_not_retried(monkeypatch, error, expected):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, b'', error + b' private-test-token https://user:private@proxy.example')

    monkeypatch.setattr(update.subprocess, 'run', run)
    with pytest.raises(update.GitHubError) as result:
        update.github('GET', update.API + '/workflows/daily.yml')
    assert expected in result.value.detail
    assert 'private' not in result.value.detail
    assert not result.value.ambiguous
    assert len(calls) == 1


def test_timeout_never_repeats_dispatch_and_updater_resolves_it(tmp_path, monkeypatch):
    calls = []
    request_id = str(uuid.uuid4())

    def run(args, **kwargs):
        calls.append(args)
        method = args[args.index('--method') + 1]
        if method == 'POST':
            raise subprocess.TimeoutExpired(args, 10)
        if any('/runs?' in arg for arg in args):
            data = {'workflow_runs': [{'id': 123, 'display_title': 'Manual papers ' + request_id}]}
        elif any(arg.endswith('/runs/123') for arg in args):
            data = {'status': 'queued'}
        else:
            data = {'state': 'active'}
        return subprocess.CompletedProcess(args, 0, json.dumps(data).encode(), b'')

    monkeypatch.setattr(update.subprocess, 'run', run)
    updater = update.DailyUpdater(tmp_path)
    assert updater.start(request_id)['state'] == 'confirming'
    assert updater.snapshot()['run_id'] == '123'
    updater.start(uuid.uuid4())
    assert sum('POST' in args for args in calls) == 1


def test_rejected_dispatch_is_immediately_reported(tmp_path):
    def remote(method, *args):
        if method == 'POST':
            raise update.GitHubError('当前 GitHub 账号没有执行此操作的权限。')
        return {'state': 'active'}

    data = update.DailyUpdater(tmp_path, remote).start(uuid.uuid4())
    assert data['state'] == 'failed'
    assert '权限' in data['message']


def test_publication_check_recovers_from_network_failure(monkeypatch):
    calls = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"update_run_id":"123"}'

    class Opener:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise URLError('temporary connection failure')
            return Response()

    monkeypatch.setattr(update, 'build_opener', lambda *args: Opener())
    assert update.published('123')['update_run_id'] == '123'
    assert len(calls) == 2
