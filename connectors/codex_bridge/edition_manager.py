"""Paired archive deletion, executed by one fixed serialized Pages workflow."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import threading
import time
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from src.editions import current_id, normalize_manifest, read, revision, valid_entry, write
from tools.recommendation_data import fetch
from .daily_update import API, REPO, ACTIVE, GitHubError, github

WORKFLOW = 'delete-editions.yml'


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: uuid.UUID
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    edition_ids: list[str] = Field(min_length=1, max_length=1000)


class EditionManager:
    def __init__(self, root, runtime, remote=github, public=fetch):
        self.root, self.runtime = Path(root), Path(runtime)
        self.remote, self.public = remote, public
        self.lock = threading.RLock()
        self.runner = None
        self.last_check = 0

    def _path(self, identifier):
        return self.runtime / 'edition-operations' / (str(uuid.UUID(str(identifier))) + '.json')

    def _save(self, state):
        write(self._path(state['request_id']), state)
        latest = self._latest()
        if latest.get('started_at', 0) <= state.get('started_at', 0):
            write(self.runtime / 'edition-operation.json', {'request_id': state['request_id']})
        return state

    def _latest(self):
        identifier = read(self.runtime / 'edition-operation.json').get('request_id')
        return read(self._path(identifier)) if identifier else {'state': 'idle'}

    def _manifest(self):
        raw = self.remote('GET', f'repos/{REPO}/contents/data/editions/index.json?ref=main')
        value = normalize_manifest(json.loads(base64.b64decode(raw['content'])))
        if not all(valid_entry(e) for e in value['editions'] + value['deleted']):
            raise HTTPException(503, '批次索引不完整，请稍后重试。')
        write(self.runtime / 'edition-manifest.json', value)
        return value

    def snapshot(self):
        with self.lock:
            value = self._manifest()
            return {'editions': value['editions'], 'current_id': current_id(value),
                    'revision': revision(value), 'operation': self._latest()}

    def submit(self, request):
        with self.lock:
            identifier = str(request.request_id)
            old = read(self._path(identifier))
            ids = sorted(set(request.edition_ids))
            if old:
                if old['edition_ids'] != ids or old['revision'] != request.revision:
                    raise HTTPException(409, '请求编号已用于另一组批次。')
                return old
            active = self._latest()
            if active['state'] in ACTIVE:
                raise HTTPException(409, '上一项删除仍在处理，请等待发布完成。')
            value = self._manifest()
            if request.revision != revision(value):
                raise HTTPException(409, '批次列表已变化，请刷新后重新选择。')
            if current_id(value) in ids:
                raise HTTPException(409, '当前推荐批次不能删除。')
            if not set(ids).issubset({e['id'] for e in value['editions']}):
                raise HTTPException(409, '所选批次不存在，请刷新后重新选择。')
            state = {'request_id': identifier, 'revision': request.revision, 'edition_ids': ids,
                     'state': 'confirming', 'started_at': time.time(), 'message': '正在提交批次删除…'}
            self._save(state)
            return self._dispatch(state)

    def _dispatch(self, state):
        state.update(state='confirming', started_at=time.time(), message='正在提交批次删除…')
        state.pop('run_id', None)
        self._save(state)
        try:
            result = self.remote('POST', API + '/workflows/' + WORKFLOW + '/dispatches', {
                'ref': 'main', 'return_run_details': True, 'inputs': {'request_id': state['request_id'],
                'revision': state['revision'], 'edition_ids': json.dumps(state['edition_ids'])}})
            if result.get('workflow_run_id'):
                state.update(run_id=str(int(result['workflow_run_id'])), state='queued', message='删除已提交，等待处理。')
        except HTTPException as exc:
            if isinstance(exc, GitHubError) and not exc.ambiguous:
                state.update(state='failed', message=exc.detail)
            else:
                state['message'] = '提交响应中断，正在确认任务，请勿重复提交。'
        return self._save(state)

    def retry(self, identifier):
        with self.lock:
            state = self.status(identifier, force=True)
            if state['state'] != 'failed':
                return state
            active = self._latest()
            if active['state'] in ACTIVE and active['request_id'] != str(identifier):
                raise HTTPException(409, '另一项删除仍在处理。')
            return self._dispatch(state)

    def status(self, identifier, force=False):
        with self.lock:
            state = read(self._path(identifier))
            if not state:
                raise HTTPException(404, '未找到删除任务。')
            if state['state'] == 'succeeded' or (not force and time.monotonic() - self.last_check < 5):
                return state
            self.last_check = time.monotonic()
            try:
                value = normalize_manifest(self.public('editions/index.json?operation=' + state['request_id']))
                operation = value['operations'].get(state['request_id'], {})
                if (operation.get('edition_ids') == state['edition_ids'] and
                        not set(state['edition_ids']).intersection(e['id'] for e in value['editions'])):
                    write(self.runtime / 'edition-manifest.json', value)
                    state.update(state='succeeded', message=f'已删除 {len(state["edition_ids"])} 批，相关论文可重新参与推荐。')
                    return self._save(state)
            except Exception:
                pass  # Publication errors never manufacture successful deletion.
            if state['state'] == 'failed':
                return state
            if not state.get('run_id'):
                runs = self.remote('GET', API + '/workflows/' + WORKFLOW + '/runs?event=workflow_dispatch&branch=main&per_page=100')
                found = next((r for r in runs.get('workflow_runs', []) if r.get('display_title') == 'Delete editions ' + state['request_id']), None)
                if found:
                    state['run_id'] = str(int(found['id']))
                elif time.time() - state['started_at'] > 180:
                    state.update(state='failed', message='未找到已提交任务，可重试；重复请求不会重复删除。')
                if not found:
                    return self._save(state)
            run = self.remote('GET', API + '/runs/' + state['run_id'])
            if run['status'] == 'completed':
                if run.get('conclusion') == 'success':
                    state.update(state='publishing', message='正在核对线上归档，完成后自动刷新。')
                    if time.time() - state['started_at'] > 1800:
                        state.update(state='failed', message='线上核对暂未完成，可重试发布。')
                else:
                    state.update(state='failed', message='删除或发布未完成，可重试。若批次列表已变化，请刷新后重新选择。')
            elif run['status'] in ('queued', 'waiting', 'requested', 'pending'):
                state.update(state='queued', message='删除任务正在排队。')
            else:
                state.update(state='running', message='正在删除所选批次并发布归档…')
            return self._save(state)

    async def run(self):
        while True:
            await asyncio.sleep(20)
            state = self._latest()
            if state['state'] in ACTIVE:
                try:
                    await asyncio.to_thread(self.status, state['request_id'])
                except Exception:
                    pass  # Retain operation for the next connectivity check.

    def start(self):
        self.runner = asyncio.create_task(self.run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
