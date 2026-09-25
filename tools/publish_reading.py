"""Publish validated public reading notes without touching the active checkout."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from urllib.request import getproxies

from src.auto_reading import public_analysis

LOCK = threading.Lock()


def publish(root, values, *, statuses=None):
    outputs = [public_analysis(value) for value in values]
    return publish_values(root, outputs, 'auto-reading', 'paper_id', statuses=statuses)


def publish_values(root, outputs, directory, key, *, statuses=None):
    if (directory, key) not in (('auto-reading', 'paper_id'), ('ai-readings', 'id'), ('reading-status', 'paper_id')):
        raise ValueError('Unsupported public notes')
    from src.auto_reading import public_status
    statuses = [public_status(v) for v in (statuses or [])]
    if not outputs and not statuses:
        return
    env = dict(os.environ)
    if not any(k.lower() == 'https_proxy' for k in env):
        for scheme, proxy in getproxies().items():
            if scheme in ('http', 'https'):
                env[scheme.upper() + '_PROXY'] = proxy
    def git(*args, data=None, extra=None):
        result = subprocess.run(['git', '-c', 'http.version=HTTP/1.1', '-c', 'http.sslBackend=openssl', *args],
            cwd=root, input=data, capture_output=True, encoding='utf-8', env={**env, **(extra or {})}, timeout=45,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        if result.returncode:
            raise RuntimeError('精读发布连接未完成，将保留结果并重试。')
        return result.stdout.strip()
    with LOCK:
        # Normal fast-forward pushes reject races with other connector publishers.
        git('fetch', 'origin', 'connector-data')
        parent = git('rev-parse', 'FETCH_HEAD')
        with tempfile.TemporaryDirectory(prefix='reading-publish-', dir=root / '.local') as temp:
            index = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
            git('read-tree', parent, extra=index)
            for value in outputs:
                blob = git('hash-object', '-w', '--stdin', data=json.dumps(value, ensure_ascii=False, indent=2), extra=index)
                git('update-index', '--add', '--cacheinfo', '100644', blob,
                    'data/' + directory + '/' + value[key] + '.json', extra=index)
            for value in statuses:
                blob = git('hash-object', '-w', '--stdin', data=json.dumps(value, ensure_ascii=False, indent=2), extra=index)
                git('update-index', '--add', '--cacheinfo', '100644', blob,
                    'data/reading-status/' + value['paper_id'] + '.json', extra=index)
            tree = git('write-tree', extra=index)
            if tree != git('rev-parse', parent + '^{tree}'):
                commit = git('commit-tree', tree, '-p', parent, '-m', 'chore: publish validated Chinese reading notes', extra=index)
                git('push', 'origin', commit + ':refs/heads/connector-data')
        from connectors.codex_bridge.daily_update import github, API
        github('POST', API + '/workflows/deploy-site.yml/dispatches', {'ref': 'main'})
