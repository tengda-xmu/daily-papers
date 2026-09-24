"""One fixed daily workflow, callable only through the paired local bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from urllib.request import Request, getproxies, urlopen

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

REPO = 'tengda-xmu/daily-papers'
API = f'repos/{REPO}/actions'
PUBLIC = 'https://tengda-xmu.github.io/daily-papers/'
ACTIVE = {'confirming', 'queued', 'running', 'publishing'}


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: uuid.UUID


def github(method, endpoint, body=None):
    executable = shutil.which('gh') or r'C:\Program Files\GitHub CLI\gh.exe'
    env = dict(os.environ)
    for scheme, proxy in getproxies().items():
        if scheme in ('http', 'https'):
            env.setdefault(scheme.upper() + '_PROXY', proxy)
    env['GODEBUG'] = 'http2client=0'
    args = [executable, 'api', '--hostname', 'github.com', '--method', method,
            '-H', 'X-GitHub-Api-Version: 2022-11-28', endpoint]
    if body is not None:
        args += ['--input', '-']
    try:
        result = subprocess.run(args, input=json.dumps(body).encode() if body is not None else None,
            capture_output=True, timeout=25, env=env,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
        if result.returncode:
            raise OSError('GitHub request failed')
        return json.loads(result.stdout or b'{}')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        # Command output may contain credentials or private proxy information.
        raise HTTPException(503, '暂时无法连接 GitHub，请检查本机 gh 登录、仓库权限与网络后重试。') from None


def published(run_id):
    request = Request(PUBLIC + 'data.json?update=' + str(run_id), headers={'Cache-Control': 'no-cache'})
    with urlopen(request, timeout=15) as response:
        content = response.read(20 * 1024 * 1024 + 1)
    if len(content) > 20 * 1024 * 1024:
        raise ValueError('Published data too large')
    return json.loads(content)


class DailyUpdater:
    def __init__(self, runtime, remote=github, fetch=published):
        self.runtime = Path(runtime)
        self.path = self.runtime / 'daily-update.json'
        self.remote, self.fetch = remote, fetch
        self.lock = threading.RLock()
        self.last_check = 0

    def _read(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'state': 'idle'}

    @staticmethod
    def _write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)

    def _save(self, data):
        self._write(self.path, data)
        return data.copy()

    @staticmethod
    def _run(data, run_id):
        run_id = int(run_id)
        if run_id <= 0:
            raise ValueError('Invalid run ID')
        data.update(run_id=str(run_id), run_url=f'https://github.com/{REPO}/actions/runs/{run_id}')

    def start(self, request_id):
        request_id = str(uuid.UUID(str(request_id)))
        with self.lock:
            old = self._read()
            # Keep retries and simultaneous tabs on the same remote job.
            if old.get('request_id') == request_id or old['state'] in ACTIVE:
                return old
            # Check authentication/network before attempting a non-repeatable POST.
            self.remote('GET', API + '/workflows/daily.yml')
            data = {'state': 'confirming', 'request_id': request_id, 'started_at': time.time(),
                    'message': '正在确认更新任务，请稍候。'}
            self._save(data)
            try:
                response = self.remote('POST', API + '/workflows/daily.yml/dispatches', {
                    'ref': 'main', 'return_run_details': True,
                    'inputs': {'request_id': request_id, 'send_digest': False}})
                if response.get('workflow_run_id'):
                    self._run(data, response['workflow_run_id'])
                    data.update(state='queued', message='更新已启动，等待开始采集。')
            except HTTPException:
                # A lost response does not prove dispatch failed. Resolve it by
                # the unique run-name before allowing another update.
                data['message'] = '提交响应中断，正在确认任务是否已启动，请勿重复提交。'
            return self._save(data)

    def snapshot(self):
        with self.lock:
            data = self._read()
            if data['state'] not in ACTIVE or time.monotonic() - self.last_check < 5:
                return data
            if not data.get('run_id'):
                runs = self.remote('GET', API + '/workflows/daily.yml/runs?event=workflow_dispatch&branch=main&per_page=30')
                found = next((r for r in runs.get('workflow_runs', [])
                              if r.get('display_title') == 'Manual papers ' + data['request_id']), None)
                if not found:
                    if time.time() - data['started_at'] > 180:
                        data.update(state='failed', message='未找到已提交的更新任务，请检查 GitHub 登录和网络后重试。')
                    self.last_check = time.monotonic()
                    return self._save(data)
                self._run(data, found['id'])
            run = self.remote('GET', API + '/runs/' + data['run_id'])
            if run['status'] == 'completed':
                if run.get('conclusion') != 'success':
                    data.update(state='failed', message='本次更新未完成，当前推荐仍可阅读；可查看运行记录后重试。')
                else:
                    self._finish(data)
            elif run['status'] in ('queued', 'waiting', 'requested', 'pending'):
                data.update(state='queued', message='更新任务正在排队，完成后会自动载入核心推荐和扩展阅读。')
            else:
                data.update(state='running', message='正在重新采集和筛选文献，完成后自动更新页面。')
                jobs = self.remote('GET', API + '/runs/' + data['run_id'] + '/jobs?per_page=10')
                names = [s['name'] for j in jobs.get('jobs', []) for s in j.get('steps', []) if s.get('status') == 'in_progress']
                if any('Deploy' in name or 'Publish' in name or 'Commit' in name for name in names):
                    data.update(state='publishing', message='文献筛选已完成，正在发布最新推荐。')
                elif any('build_site' in name for name in names):
                    data['message'] = '正在生成核心推荐、扩展阅读和历史归档。'
            self.last_check = time.monotonic()
            return self._save(data)

    def _finish(self, data):
        data.update(state='publishing', message='发布已完成，正在核对线上数据，稍后自动刷新。')
        try:
            payload = self.fetch(data['run_id'])
            if (str(payload.get('update_run_id')) != data['run_id'] or not payload.get('generated_at')
                    or not isinstance(payload.get('core'), list) or not isinstance(payload.get('extended'), list)):
                return  # Old CDN data is never treated as a successful update.
        except Exception:
            return  # Transient publication delay; subsequent status calls retry.
        previous = {}
        try:
            previous = json.loads((self.runtime / 'recommendations.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        # Retain prior paper metadata so existing conversations still resolve.
        self._write(self.runtime / 'recommendation-history' / (data['run_id'] + '.json'), payload)
        self._write(self.runtime / 'recommendations.json', payload)
        changed = any([p.get('id') for p in previous.get(section, [])] != [p.get('id') for p in payload[section]]
                      for section in ('core', 'extended')) if previous else None
        data.update(state='succeeded', generated_at=payload['generated_at'], core_count=len(payload['core']),
                    extended_count=len(payload['extended']), changed=changed,
                    message=f'更新完成：核心推荐 {len(payload["core"])} 篇，扩展阅读 {len(payload["extended"])} 篇。')
