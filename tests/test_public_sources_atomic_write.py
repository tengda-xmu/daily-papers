from concurrent.futures import ThreadPoolExecutor
import errno
import json
from pathlib import Path
import threading

import pytest

from src import public_sources


def sharing_error(code):
    error = OSError('File is temporarily in use')
    error.winerror = code
    return error


@pytest.mark.parametrize('error', [PermissionError('Access denied'), sharing_error(32), sharing_error(33)])
def test_atomic_write_recovers_after_temporary_replacement_lock(tmp_path, monkeypatch, error):
    destination = tmp_path / 'coverage.json'
    destination.write_text('{"confirmed": "old"}', encoding='utf-8')
    replace = Path.replace
    attempts = []
    sleeps = []

    def locked_then_replace(source, target):
        assert json.loads(destination.read_text(encoding='utf-8')) == {'confirmed': 'old'}
        attempts.append(source)
        if len(attempts) < 3:
            raise error
        return replace(source, target)

    monkeypatch.setattr(Path, 'replace', locked_then_replace)
    monkeypatch.setattr(public_sources.time, 'sleep', sleeps.append)
    public_sources.write(destination, {'confirmed': '公众号报道'})

    assert public_sources.read(destination) == {'confirmed': '公众号报道'}
    assert len(sleeps) == 2
    assert len(set(attempts)) == 1
    assert list(tmp_path.iterdir()) == [destination]


def test_persistent_permission_error_preserves_previous_success_and_is_raised(tmp_path, monkeypatch):
    destination = tmp_path / 'coverage.json'
    previous = b'{"confirmed": ["existing source"]}\n'
    destination.write_bytes(previous)
    error = PermissionError('Destination stays read-only')
    attempts = []
    sleeps = []

    def denied(source, target):
        attempts.append(source)
        assert destination.read_bytes() == previous
        raise error

    monkeypatch.setattr(Path, 'replace', denied)
    monkeypatch.setattr(public_sources.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError) as raised:
        public_sources.write(destination, {'confirmed': ['new source']})

    assert raised.value is error
    assert 1 < len(attempts) <= 10
    assert len(sleeps) == len(attempts) - 1 and sum(sleeps) < 5
    assert destination.read_bytes() == previous
    assert list(tmp_path.iterdir()) == [destination]


def test_unrelated_filesystem_error_is_not_retried(tmp_path, monkeypatch):
    destination = tmp_path / 'coverage.json'
    destination.write_text('{"old": true}', encoding='utf-8')
    error = OSError(errno.ENOSPC, 'No space left')
    sleeps = []

    def fail(source, target):
        raise error

    monkeypatch.setattr(Path, 'replace', fail)
    monkeypatch.setattr(public_sources.time, 'sleep', sleeps.append)
    with pytest.raises(OSError) as raised:
        public_sources.write(destination, {'new': True})
    assert raised.value is error
    assert not sleeps
    assert public_sources.read(destination) == {'old': True}
    assert list(tmp_path.iterdir()) == [destination]


def test_concurrent_writers_use_distinct_complete_temporary_files(tmp_path, monkeypatch):
    destination = tmp_path / 'coverage.json'
    destination.write_text('{"old": true}', encoding='utf-8')
    payloads = [{'writer': 1, 'items': ['a'] * 100}, {'writer': 2, 'items': ['b'] * 100}]
    replace = Path.replace
    barrier = threading.Barrier(2)
    staged = {}
    lock = threading.Lock()

    def synchronized_replace(source, target):
        with lock:
            first_attempt = source not in staged
            staged[source] = json.loads(source.read_text(encoding='utf-8'))
        if first_attempt:
            barrier.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(Path, 'replace', synchronized_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda value: public_sources.write(destination, value), payloads))

    assert len(staged) == 2
    assert sorted(staged.values(), key=lambda value: value['writer']) == payloads
    assert public_sources.read(destination) in payloads
    assert list(tmp_path.iterdir()) == [destination]
