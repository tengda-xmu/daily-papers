"""Paired browser CRUD with explicit publication of manual subscriptions."""
import base64
import hashlib
import json
from pathlib import Path
import threading
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from src.wechat_subscriptions import (LOCAL_PATH, PUBLIC_PATH, MAX_ACCOUNTS, clean_accounts,
    effective_accounts, name_key, published_accounts)
from .journals import github, REPO


class SubscriptionChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(max_length=64)
    id: str = Field(default='', pattern=r'^(?:[a-f0-9]{32})?$')
    name: str = Field(min_length=1, max_length=100)
    alias: str = Field(default='', max_length=80)
    group: str = Field(default='科研综合', min_length=1, max_length=60)
    enabled: StrictBool = True


def remote_accounts(method, body=None):
    return github(method, body, endpoint=f'repos/{REPO}/contents/{PUBLIC_PATH}')


def merge_accounts(base, local, remote):
    base, local, remote = ({row['id']: row for row in rows} for rows in (base, local, remote))
    merged = dict(remote)
    for key in base.keys() | local.keys():
        if base.get(key) == local.get(key):
            continue
        if remote.get(key) not in (base.get(key), local.get(key)):
            raise HTTPException(409, '同一订阅已在云端修改，请先核对远程配置；本机修改已保留。')
        if key in local:
            merged[key] = local[key]
        else:
            merged.pop(key, None)
    return clean_accounts(list(merged.values()))


class SubscriptionManager:
    def __init__(self, root, remote=remote_accounts):
        self.root = Path(root)
        self.path = self.root / LOCAL_PATH
        self.remote = remote
        self.lock = threading.RLock()

    def _read(self):
        if self.path.exists():
            state = json.loads(self.path.read_text(encoding='utf-8'))
            state['accounts'] = clean_accounts(state['accounts'])
            state['base'] = clean_accounts(state['base'])
            return state
        rows = published_accounts(self.root)
        return {'accounts': rows, 'base': rows}

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
            raise HTTPException(409, '订阅列表已变化，请刷新列表后再保存；填写内容仍保留。')

    def snapshot(self):
        with self.lock:
            state = self._read()
            policy = self.root / 'config/wechat_accounts.json'
            groups = json.loads(policy.read_text(encoding='utf-8')).get('groups', []) if policy.exists() else []
            return {'accounts': state['accounts'], 'revision': self.revision(state),
                'pending': state['base'] != state['accounts'], 'max_accounts': MAX_ACCOUNTS,
                'existing': [row for row in effective_accounts(self.root, manual=[])],
                'groups': [row['name'] for row in groups] + ['科研综合'],
                'commit_url': state.get('commit_url', '')}

    def save(self, data):
        with self.lock:
            state = self._read(); self._check(state, data.revision)
            if data.id and not any(row['id'] == data.id for row in state['accounts']):
                raise HTTPException(404, '这条订阅已不存在，请刷新列表。')
            row = data.model_dump(exclude={'revision'})
            row['id'] = row['id'] or uuid.uuid4().hex
            row = clean_accounts([row])[0]
            previous = next((r for r in state['accounts'] if r['id'] == data.id), None)
            for existing in effective_accounts(self.root, manual=[]):
                # An automatic discovery can later identify the same manual
                # subscription. Its original entry must remain editable.
                if previous and (name_key(previous['name']) == name_key(existing['name']) or
                        (previous['alias'] and name_key(previous['alias']) == name_key(existing.get('alias', '')))):
                    continue
                if name_key(row['name']) == name_key(existing['name']) or (row['alias'] and name_key(row['alias']) == name_key(existing.get('alias', ''))):
                    raise ValueError('这个公众号已在现有订阅目录中，无需重复添加。')
            state['accounts'] = clean_accounts([r for r in state['accounts'] if r['id'] != row['id']] + [row])
            self._write(state)
            return self.snapshot()

    def remove(self, identifier, revision):
        with self.lock:
            state = self._read(); self._check(state, revision)
            state['accounts'] = [row for row in state['accounts'] if row['id'] != identifier]
            self._write(state)
            return self.snapshot()

    def sync(self, revision):
        with self.lock:
            state = self._read(); self._check(state, revision)
            remote = self.remote('GET')
            rows = clean_accounts(json.loads(base64.b64decode(remote['content']))['accounts'])
            merged = merge_accounts(state['base'], state['accounts'], rows)
            if merged != rows:
                payload = json.dumps({'accounts': merged}, ensure_ascii=False, indent=2) + '\n'
                result = self.remote('PUT', {'branch': 'main', 'sha': remote['sha'],
                    'message': 'chore: update manual WeChat subscriptions',
                    'content': base64.b64encode(payload.encode()).decode()})
                state['commit_url'] = result.get('commit', {}).get('html_url', '')
            state.update(base=merged, accounts=merged)
            self._write(state)
            return self.snapshot()
