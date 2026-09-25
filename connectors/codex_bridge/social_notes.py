"""Local note drafts and explicit publication of short, attributed metadata."""
import base64
import hashlib
import json
import threading
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from src.public_sources import clean, read, write
from src.social_content import MANUAL, route
from src.xiaohongshu import share_url, note_id, public_url, read_note
from .journals import github, REPO


class NoteChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(default='', max_length=64)
    id: str = Field(default='', pattern=r'^(?:xhs-[a-f0-9]{24}|xhs-pending-[a-f0-9]{20})?$')
    share: str = Field(min_length=1, max_length=2500)
    title: str = Field(default='', max_length=300)
    author: str = Field(default='', max_length=100)
    body: str = Field(default='', max_length=10000)
    published_at: str = Field(default='', pattern=r'^(?:20\d{2}-\d{2}-\d{2})?$')


def remote_notes(method, body=None):
    return github(method, body, endpoint=f'repos/{REPO}/contents/{MANUAL}')


class NoteManager:
    def __init__(self, root, remote=remote_notes, reader=read_note):
        self.root, self.remote, self.reader = root, remote, reader
        self.path = root / '.local/social-notes/state.json'
        self.lock = threading.RLock()

    def _read(self):
        initial = read(self.root / MANUAL).get('entries', [])
        return read(self.path, {'entries':initial, 'base':initial})

    def revision(self, state):
        return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()

    def check(self, state, revision):
        if revision != self.revision(state):
            raise HTTPException(409, '笔记列表已变化，请刷新后重试；填写内容仍保留。')

    @staticmethod
    def public(rows):
        return [r for raw in rows if (r := route(raw))]

    def snapshot(self):
        with self.lock:
            state = self._read()
            return {'entries':state['entries'], 'revision':self.revision(state),
                    'pending':self.public(state['entries']) != state['base']}

    def preview(self, data):
        url = share_url(data.share)
        row = {'url':public_url(url), 'title':'', 'author':'', 'published_at':'', 'evidence_text':'',
               'verification':'pending', 'evidence_kind':'search_snippet', 'read_status':'unavailable'}
        try:
            row.update(self.reader(url), read_status='readable')
        except Exception as exc:
            if note_id(getattr(exc,'resolved_url','')):
                row['url'] = public_url(exc.resolved_url)
        if data.title:
            row['title'] = clean(data.title)
        if data.author:
            row['author'] = clean(data.author)
        if data.published_at:
            from datetime import date
            date.fromisoformat(data.published_at)
            row['published_at'] = data.published_at
        if data.body:
            row.update(evidence_text=clean(data.body), evidence_kind='manual_text', read_status='provided')
        row['title'] = row['title'] or '小红书笔记（待补充）'
        identity = note_id(row['url'])
        row.update(id='xhs-'+identity if identity else 'xhs-pending-'+hashlib.sha256(row['url'].encode()).hexdigest()[:20],
                   article_id=identity, platform='xiaohongshu', provider='xiaohongshu', source=row['author'] or '小红书',
                   checked_at=datetime.now(timezone.utc).isoformat())
        result = route(row)
        if not result:
            raise ValueError('这条内容不属于科研线索或 AI 前沿的收录范围。')
        # Full manual text is local only; publication uses the short validated projection.
        result.update(share=url, body=data.body)
        return result

    def save(self, data):
        with self.lock:
            state = self._read(); self.check(state, data.revision)
            if data.id and not any(r['id'] == data.id for r in state['entries']):
                raise HTTPException(404, '笔记已不存在，请刷新列表。')
            row = self.preview(data)
            state['entries'] = [r for r in state['entries'] if r['id'] not in (data.id, row['id'])] + [row]
            if len(state['entries']) > 500:
                raise ValueError('手动笔记达到 500 条，请先整理已有条目。')
            write(self.path, state)
            return self.snapshot()

    def remove(self, identity, revision):
        with self.lock:
            state = self._read(); self.check(state, revision)
            state['entries'] = [r for r in state['entries'] if r['id'] != identity]
            write(self.path, state)
            return self.snapshot()

    def sync(self, revision):
        with self.lock:
            state = self._read(); self.check(state, revision)
            remote = self.remote('GET')
            values = json.loads(base64.b64decode(remote['content']))['entries']
            base, local, cloud = ({r['id']: r for r in rows} for rows in (state['base'], self.public(state['entries']), values))
            merged = dict(cloud)
            for key in base.keys() | local.keys():
                if base.get(key) == local.get(key):
                    continue
                if cloud.get(key) not in (base.get(key), local.get(key)):
                    raise HTTPException(409, '同一笔记已在网站修改，请核对后重试；本机草稿保留。')
                if key in local:
                    merged[key] = local[key]
                else:
                    merged.pop(key, None)
            public = list(merged.values())
            if public != values:
                self.remote('PUT', {'branch':'main', 'sha':remote['sha'], 'message':'chore: sync public research notes',
                    'content':base64.b64encode((json.dumps({'entries':public},ensure_ascii=False,indent=2)+'\n').encode()).decode()})
            draft = {r['id']: r for r in state['entries']}
            state.update(base=public, entries=[{**r, **({'body':draft[r['id']].get('body',''), 'share':draft[r['id']].get('share',r['url'])} if r['id'] in draft else {})} for r in public])
            write(self.path, state)
            return self.snapshot()
