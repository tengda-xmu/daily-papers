"""One fixed daily workflow, callable only through the paired local bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, getproxies

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

REPO = 'tengda-xmu/daily-papers'
API = f'repos/{REPO}/actions'
PUBLIC = 'https://tengda-xmu.github.io/daily-papers/'
ACTIVE = {'confirming', 'queued', 'running', 'publishing'}
_prefer_system_proxy = False


class UpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: uuid.UUID


class GitHubError(HTTPException):
    def __init__(self, detail, *, ambiguous=False):
        super().__init__(503, detail)
        self.ambiguous = ambiguous


def _network_environments():
    """Honor explicit proxy choices; otherwise try gh's normal route and Windows' proxy."""
    env = dict(os.environ)
    env['GODEBUG'] = 'http2client=0'
    if any(key.lower() in ('http_proxy', 'https_proxy', 'all_proxy') for key in env):
        return [(False, env), (False, env)]
    system = dict(env)
    for scheme, proxy in getproxies().items():
        if scheme in ('http', 'https'):
            system[scheme.upper() + '_PROXY'] = proxy
    routes = [(False, env), (True, system)] if system != env else [(False, env), (False, env)]
    return list(reversed(routes)) if _prefer_system_proxy else routes


def _github_failure(output):
    """Classify privately; never return gh output, URLs, tokens or proxy credentials."""
    text = output.decode(errors='replace').lower()
    match = re.search(r'\bhttp\s+(\d{3})\b', text)
    status = int(match[1]) if match else None
    if status == 401 or any(s in text for s in ('gh auth login', 'not logged into', 'authentication required', 'bad credentials')):
        return 'GitHub 登录已失效或尚未登录，请在本机运行 gh auth login 后重试。', False
    if status == 429 or (status == 403 and any(s in text for s in ('rate limit', 'abuse detection'))):
        return 'GitHub 请求暂时受限，请稍后重试；当前推荐仍可阅读。', False
    if status == 403:
        return '当前 GitHub 账号没有执行此操作的权限，请检查仓库与 Actions 授权。', False
    if status == 404:
        return 'GitHub 仓库或更新工作流不可访问，请检查当前账号的仓库权限。', False
    if status and 400 <= status < 500:
        return 'GitHub 拒绝了更新请求，请检查工作流配置后重试。', False
    if any(s in text for s in ('certificate', 'x509')):
        return 'GitHub 安全连接验证失败，请检查本机代理与证书配置后重试。', True
    return '连接 GitHub 暂时中断，已尝试重新连接，请稍后重试。', True


def github(method, endpoint, body=None):
    global _prefer_system_proxy
    executable = shutil.which('gh') or r'C:\Program Files\GitHub CLI\gh.exe'
    args = [executable, 'api', '--hostname', 'github.com', '--method', method,
            '-H', 'X-GitHub-Api-Version: 2022-11-28', endpoint]
    if body is not None:
        args += ['--input', '-']
    routes = _network_environments()
    # A dispatch or config write may have succeeded despite a lost response.
    # Retry reads only; DailyUpdater resolves uncertain dispatches by request ID.
    if method not in ('GET', 'HEAD'):
        routes = routes[:1]
    for system_proxy, env in routes:
        try:
            result = subprocess.run(args, input=json.dumps(body).encode() if body is not None else None,
                capture_output=True, timeout=10, env=env,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            if result.returncode:
                detail, transient = _github_failure(result.stderr + result.stdout)
                if not transient:
                    raise GitHubError(detail)
            else:
                payload = json.loads(result.stdout or b'{}')
                _prefer_system_proxy = system_proxy
                return payload
        except FileNotFoundError:
            raise GitHubError('未找到 GitHub CLI（gh），请安装后重新启动本机论文助手。') from None
        except subprocess.TimeoutExpired:
            detail = '连接 GitHub 超时，已尝试重新连接，请检查网络后重试。'
        except OSError:
            raise GitHubError('本机无法启动 GitHub CLI（gh），请检查安装后重新启动论文助手。') from None
        except ValueError:
            detail = 'GitHub 返回的数据不完整，已尝试重新连接，请稍后重试。'
    raise GitHubError(detail, ambiguous=True) from None


def published(run_id):
    request = Request(PUBLIC + 'data.json?update=' + str(run_id), headers={'Cache-Control': 'no-cache'})
    for _, env in _network_environments():
        proxies = {key.lower()[:-6]: value for key, value in env.items() if key.lower().endswith('_proxy')}
        try:
            with build_opener(ProxyHandler(proxies)).open(request, timeout=10) as response:
                content = response.read(20 * 1024 * 1024 + 1)
            if len(content) > 20 * 1024 * 1024:
                raise ValueError('Published data too large')
            return json.loads(content)
        except HTTPError as exc:
            if exc.code < 500:
                raise
        except (URLError, TimeoutError, ConnectionError):
            pass
    raise OSError('Published data temporarily unavailable')


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
            except HTTPException as exc:
                if isinstance(exc, GitHubError) and not exc.ambiguous:
                    data.update(state='failed', message=exc.detail)
                    return self._save(data)
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
