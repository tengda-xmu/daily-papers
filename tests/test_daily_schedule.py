import base64
from datetime import datetime
import json
from pathlib import Path

import pytest
import yaml

from tools.daily_schedule import dispatch_if_needed, update_needed, workflow_gate

NOW = datetime.fromisoformat('2026-09-25T07:20:00+08:00')


def edition(timestamp='2026-09-24T23:02:00Z'):
    return {'generated_at': timestamp, 'core': [{'id': '111111111111'}], 'extended': []}


def test_beijing_morning_boundary_and_manual_updates():
    assert update_needed({}, datetime.fromisoformat('2026-09-24T22:59:59Z')) == (False, 'before_morning_update')
    assert update_needed(edition('2026-09-24T22:59:00Z'), NOW)[0]
    assert update_needed(edition(), NOW) == (False, 'already_updated_today')
    assert workflow_gate(edition(), 'schedule', now=NOW)[0] is False
    assert workflow_gate(edition(), 'workflow_dispatch', scheduled_check=True, now=NOW)[0] is False
    assert workflow_gate(edition(), 'workflow_dispatch', now=NOW) == (True, 'manual_update')
    tomorrow = datetime.fromisoformat('2026-09-26T07:00:00+08:00')
    assert update_needed(edition(), tomorrow)[0]


@pytest.mark.parametrize('payload', [
    {}, {'generated_at': 'invalid'}, edition('2026-09-25T12:00:00Z'),
    edition('2026-09-25T07:02:00'), {**edition(), 'core': []},
    {**edition(), 'extended': None}, None,
])
def test_incomplete_invalid_or_future_data_cannot_skip_update(payload):
    assert update_needed(payload, NOW)[0]


class Remote:
    def __init__(self, payload, runs=()):
        self.payload, self.runs, self.calls = payload, runs, []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == 'POST':
            return {'workflow_run_id': 123}
        if '/contents/' in path:
            return {'content': base64.b64encode(json.dumps(self.payload).encode()).decode()}
        return {'workflow_runs': self.runs}


def test_current_edition_skips_without_dispatch_and_active_runs_are_respected():
    remote = Remote(edition())
    assert dispatch_if_needed(remote, NOW)['state'] == 'already_updated_today'
    assert len(remote.calls) == 1
    for state in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
        remote = Remote({}, [{'status': state}])
        assert dispatch_if_needed(remote, NOW)['state'] == 'update_already_running'
        assert not any(method == 'POST' for method, _, _ in remote.calls)


def test_missing_or_failed_edition_dispatches_guarded_workflow_once():
    remote = Remote({}, [{'status': 'completed', 'conclusion': 'failure'}])
    assert dispatch_if_needed(remote, NOW) == {'state': 'dispatched', 'run_id': 123}
    posts = [call for call in remote.calls if call[0] == 'POST']
    assert len(posts) == 1
    assert posts[0][1].endswith('/workflows/daily.yml/dispatches')
    assert posts[0][2]['inputs']['scheduled_check'] is True
    assert posts[0][2]['inputs']['send_digest'] is False


def test_large_recommendation_file_reads_git_blob_instead_of_empty_contents():
    calls = []
    def remote(method, path, body=None):
        calls.append((method, path))
        if '/contents/' in path:
            return {'encoding': 'none', 'content': '', 'sha': 'a' * 40}
        assert path.endswith('/git/blobs/' + 'a' * 40)
        return {'encoding': 'base64', 'content': base64.b64encode(json.dumps(edition()).encode()).decode()}
    assert dispatch_if_needed(remote, NOW)['state'] == 'already_updated_today'
    assert len(calls) == 2


def test_dispatch_failure_is_not_blindly_retried():
    remote = Remote({})
    def uncertain(method, path, body=None):
        result = remote(method, path, body)
        if method == 'POST':
            raise TimeoutError('response lost')
        return result
    with pytest.raises(TimeoutError):
        dispatch_if_needed(uncertain, NOW)
    assert sum(method == 'POST' for method, _, _ in remote.calls) == 1


def test_workflow_serializes_checks_and_uses_current_main():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load((root / '.github/workflows/daily.yml').read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
    assert [entry['cron'] for entry in workflow['on']['schedule']] == ['0 23 * * *', '17,37 23 * * *', '17 0 * * *']
    assert workflow['concurrency']['cancel-in-progress'] == 'false'
    assert workflow['jobs']['update']['needs'] == 'check'
    assert "needed == 'true'" in workflow['jobs']['update']['if']
    for name in ('check', 'update'):
        assert workflow['jobs'][name]['steps'][0]['with']['ref'] == 'main'
