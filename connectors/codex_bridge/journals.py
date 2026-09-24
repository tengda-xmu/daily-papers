"""Paired browser journal management; GitHub writes target one fixed data file."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, getproxies, urlopen

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator

from src.custom_journals import (LOCAL_PATH, PUBLIC_PATH, MAX_JOURNALS, clean_journals,
    journal_groups, normalize_issn, published_journals)

REPO = 'tengda-xmu/daily-papers'
ENDPOINT = f'repos/{REPO}/contents/{PUBLIC_PATH}'


class JournalChange(BaseModel):
    revision: str = Field(max_length=64)
    issn: str = Field(max_length=16)
    name: str = Field(min_length=2, max_length=180)
    group: str = Field(default='自定义期刊', min_length=1, max_length=60)
    enabled: bool = True

    @field_validator('issn')
    @classmethod
    def valid_issn(cls, value):
        return normalize_issn(value)


class Revision(BaseModel):
    revision: str = Field(max_length=64)


def merge_journals(base, local, remote):
    base, local, remote = ({r['issn']: r for r in rows} for rows in (base, local, remote))
    merged = dict(remote)
    for key in base.keys() | local.keys():
        if base.get(key) == local.get(key):
            continue
        if remote.get(key) not in (base.get(key), local.get(key)):
            raise HTTPException(409, '同一期刊已在 GitHub 上更改，请核对远程配置后再同步；本机修改已保留。')
        if key in local:
            merged[key] = local[key]
        else:
            merged.pop(key, None)
    return clean_journals(list(merged.values()))


def github(method, body=None, *, endpoint=ENDPOINT):
    executable = shutil.which('gh') or r'C:\Program Files\GitHub CLI\gh.exe'
    env = dict(os.environ)
    for scheme, proxy in getproxies().items():
        if scheme in ('http', 'https'):
            env.setdefault(scheme.upper() + '_PROXY', proxy)
    args = [executable, 'api', '--method', method, endpoint + ('?ref=main' if method == 'GET' else '')]
    if body is not None:
        args += ['--input', '-']
    try:
        result = subprocess.run(args, input=json.dumps(body).encode() if body is not None else None,
            capture_output=True, timeout=45, env=env,
            **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
    except (OSError, subprocess.TimeoutExpired):
        raise HTTPException(503, '无法连接 GitHub CLI。请确认本机 GitHub 已登录且网络可用；本机修改仍然保留。')
    if result.returncode:
        # Do not return command output: it can contain account or proxy details.
        raise HTTPException(503, 'GitHub 同步未完成，请检查登录、仓库写入权限和网络后重试。')
    return json.loads(result.stdout)


class JournalManager:
    def __init__(self, root, remote=github):
        self.root = Path(root)
        self.path = self.root / LOCAL_PATH
        self.remote = remote
        self.lock = threading.RLock()
        self.verified = {}

    def _read(self):
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding='utf-8'))
            state['journals'] = clean_journals(state['journals'])
            state['base'] = clean_journals(state['base'])
            return state
        rows = published_journals(self.root)
        return {'base': rows, 'journals': rows}

    def _write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)

    @staticmethod
    def revision(state):
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def _check(self, state, revision):
        if revision != self.revision(state):
            raise HTTPException(409, '期刊列表已变化，请刷新列表后再保存。')

    def snapshot(self):
        with self.lock:
            state = self._read()
            return {'journals': state['journals'], 'revision': self.revision(state),
                'pending': state['base'] != state['journals'], 'max_journals': MAX_JOURNALS,
                'groups': [g['id'] for g in journal_groups(self.root)],
                'commit_url': state.get('commit_url', '')}

    def lookup(self, issn):
        issn = normalize_issn(issn)
        if issn in self.verified:
            return self.verified[issn]
        try:
            for attempt in range(2):
                try:
                    with urlopen(Request('https://api.crossref.org/journals/' + issn,
                            headers={'User-Agent': 'daily-papers/1.0 (journal subscriptions)'}), timeout=18) as response:
                        data = json.load(response)['message']
                    break
                except HTTPError:
                    raise  # Do not retry authorization errors or rate limits.
                except (URLError, TimeoutError):
                    if attempt: raise
                    time.sleep(.5)
            aliases = sorted({issn, *(normalize_issn(x) for x in data.get('ISSN', []))})
            result = {'issn': issn, 'issns': aliases, 'name': data['title'], 'publisher': data.get('publisher', '')}
        except HTTPError as exc:
            if exc.code == 404:
                raise HTTPException(400, 'Crossref 未收录这个 ISSN，请核对期刊的纸质版或电子版 ISSN。')
            raise HTTPException(503, '期刊验证暂时失败，请稍后重试。')
        except Exception:
            raise HTTPException(503, '未能验证期刊，请检查网络后重试。')
        self.verified[issn] = result
        return result

    def save(self, data):
        with self.lock:
            state = self._read()
            self._check(state, data.revision)
            existing = next((j for j in state['journals'] if j['issn'] == data.issn), None)
            verified = ({'issns': existing['issns'], 'name': existing['canonical_name']}
                        if existing else self.lookup(data.issn))
            rows = [r for r in state['journals'] if r['issn'] != data.issn]
            rows.append({'issn': data.issn, 'issns': verified['issns'], 'name': data.name, 'canonical_name': verified['name'],
                         'group': data.group, 'enabled': data.enabled})
            state['journals'] = clean_journals(rows)
            self._write(state)
            return self.snapshot()

    def remove(self, issn, revision):
        with self.lock:
            state = self._read(); self._check(state, revision)
            state['journals'] = [r for r in state['journals'] if r['issn'] != normalize_issn(issn)]
            self._write(state)
            return self.snapshot()

    def sync(self, revision):
        with self.lock:
            state = self._read(); self._check(state, revision)
            remote = self.remote('GET')
            rows = clean_journals(json.loads(base64.b64decode(remote['content']))['journals'])
            merged = merge_journals(state['base'], state['journals'], rows)
            if merged != rows:
                payload = json.dumps({'journals': merged}, ensure_ascii=False, indent=2) + '\n'
                result = self.remote('PUT', {'branch': 'main', 'sha': remote['sha'],
                    'message': 'chore: update journal subscriptions',
                    'content': base64.b64encode(payload.encode()).decode()})
                state['commit_url'] = result.get('commit', {}).get('html_url', '')
            state.update(base=merged, journals=merged)
            self._write(state)
            return self.snapshot()
