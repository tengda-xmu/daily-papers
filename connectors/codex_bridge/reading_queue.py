"""Resumable, low-priority local Codex enrichment; isolated from personal chats."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from src.auto_reading import fingerprint, prompt, public_analysis, public_paper
from src.editions import read, relative_path, selected, write
from src.reading_notes import valid_analysis


class ReadingQueue:
    def __init__(self, root, runtime, client, generation_lock, *, fetch=None, publisher=None):
        self.root, self.runtime, self.client, self.lock = Path(root), Path(runtime), client, generation_lock
        self.path = self.runtime / 'reading-queue.sqlite3'
        self.runtime.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                paper_id TEXT PRIMARY KEY, paper TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                result TEXT, error TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL,
                thread_id TEXT, draft TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 0)''')
            if 'priority' not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:
                db.execute('ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE tasks SET state='pending', attempts=MAX(0,attempts-1) WHERE state='generating'")
        self.fetch, self.publisher = fetch, publisher
        self.runner = self.active = None
        self.quiet_until = time.time() + 30
        self.last_sync = self.last_publish = 0
        self.sync_state = 'pending'

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def enqueue(self, paper, priority=0):
        if not paper.get('id'):
            return
        clean = public_paper(paper)
        key = fingerprint(clean)
        state = 'pending' if len(clean.get('abstract') or '') >= 80 and clean.get('landing_url') else 'missing_evidence'
        with self.db() as db:
            old = db.execute('SELECT * FROM tasks WHERE paper_id=?', (paper['id'],)).fetchone()
            if old:
                db.execute('UPDATE tasks SET priority=? WHERE paper_id=?', (priority, paper['id']))
            if old and (old['fingerprint'] == key or old['state'] == 'generating'):
                return
            db.execute('''INSERT INTO tasks(paper_id,paper,fingerprint,state,updated_at,priority) VALUES(?,?,?,?,?,?)
                ON CONFLICT(paper_id) DO UPDATE SET paper=excluded.paper,fingerprint=excluded.fingerprint,
                state=excluded.state,attempts=0,next_attempt=0,result=NULL,error='',thread_id=NULL,draft='',updated_at=excluded.updated_at''',
                (paper['id'], json.dumps(clean, ensure_ascii=False), key, state, time.time(), priority))

    def sync(self):
        if self.fetch is None:
            from tools.recommendation_data import fetch
        else:
            fetch = self.fetch
        manifest = fetch('editions/index.json')
        papers = {}
        for entry in sorted(manifest.get('editions', []), key=lambda e: (e['date'], e['number'])):
            path = relative_path(entry)
            local = self.runtime / 'public-editions' / entry['date'] / (entry['id'] + '.json')
            payload = read(local)
            if not payload:
                payload = fetch(path)
                if payload.get('edition') != entry:
                    raise ValueError('Public edition mismatch')
                write(local, payload)
            for paper in selected(payload):
                papers[paper['id']] = paper
        latest = fetch('data.json')
        if latest.get('edition', {}).get('id') in {e['id'] for e in manifest.get('editions', [])}:
            write(self.runtime / 'recommendations.json', latest)
            papers.update({p['id']: p for p in selected(latest)})
        # Newest metadata wins; repeated historic copies never reset a task.
        for paper in papers.values():
            self.enqueue(paper, priority=100 if paper['id'] in {p['id'] for p in selected(latest)} else 0)
        self.sync_state = 'ok'

    def snapshot(self):
        with self.db() as db:
            rows = db.execute('SELECT paper_id,paper,state,attempts,updated_at,error FROM tasks ORDER BY updated_at DESC').fetchall()
        items = [{**{k: row[k] for k in ('paper_id', 'state', 'attempts', 'updated_at', 'error')},
                  'title': json.loads(row['paper']).get('title', '')} for row in rows]
        return {'sync_state': self.sync_state, 'tasks': items,
                'counts': {state: sum(r['state'] == state for r in items) for state in
                           ('pending', 'generating', 'ready', 'published', 'retry', 'failed', 'missing_evidence')}}

    def retry(self, identifier):
        with self.db() as db:
            row = db.execute('SELECT state FROM tasks WHERE paper_id=?', (identifier,)).fetchone()
            if not row or row['state'] not in ('failed', 'retry', 'missing_evidence'):
                raise ValueError('该论文当前没有可重试的精读任务。')
            db.execute("UPDATE tasks SET state='pending',attempts=0,next_attempt=0,error='' WHERE paper_id=?", (identifier,))
        return self.snapshot()

    async def preempt(self):
        self.quiet_until = time.time() + 120
        if self.active and not self.active.done():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)

    async def generate(self, row):
        identifier, paper = row['paper_id'], json.loads(row['paper'])
        async with self.lock:
            with self.db() as db:
                db.execute("UPDATE tasks SET state='generating',attempts=attempts+1,error='',updated_at=? WHERE paper_id=?", (time.time(), identifier))
            try:
                await self.client.start()
                thread = await self.client.thread(row['thread_id'])
                with self.db() as db:
                    db.execute('UPDATE tasks SET thread_id=? WHERE paper_id=?', (thread, identifier))
                text, saved = '', 0
                async for event in self.client.turn(thread, prompt(paper)):
                    if event['type'] == 'delta':
                        text += event.get('text', '')
                        if len(text) > 50000:
                            raise ValueError('Reading output too large')
                        if len(text) - saved > 1000:
                            with self.db() as db:
                                db.execute('UPDATE tasks SET draft=? WHERE paper_id=?', (text, identifier))
                            saved = len(text)
                    elif event['type'] == 'completed' and event.get('status') != 'completed':
                        raise ValueError('Reading interrupted')
                text = text.strip()
                if text.startswith('```'):
                    text = text.split('\n', 1)[1].rsplit('```', 1)[0]
                raw = json.loads(text)
                analysis = raw.get('analysis') or {}
                analysis.update(analysis_status='ready', analysis_basis='abstract', analysis_kind='model',
                    analysis_sources=[paper['landing_url']], analyzed_at=datetime.now(timezone.utc).isoformat(),
                    llm_model=self.client.model)
                value = public_analysis({'paper_id': identifier, 'fingerprint': row['fingerprint'],
                                         'analysis': analysis, 'evaluation': raw.get('evaluation')})
                evidence = value['evaluation']['evidence']
                if evidence and evidence.casefold() not in paper['abstract'].casefold():
                    raise ValueError('Assessment evidence is not in the provided abstract')
                write(self.runtime / 'reading-results' / (identifier + '.json'), value)
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='ready',result=?,draft='',updated_at=? WHERE paper_id=?",
                               (json.dumps(value, ensure_ascii=False), time.time(), identifier))
            except asyncio.CancelledError:
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1) WHERE paper_id=?", (identifier,))
                raise
            except Exception:
                with self.db() as db:
                    attempts = db.execute('SELECT attempts FROM tasks WHERE paper_id=?', (identifier,)).fetchone()[0]
                    db.execute('UPDATE tasks SET state=?,next_attempt=?,error=?,updated_at=? WHERE paper_id=?',
                        ('failed' if attempts >= 3 else 'retry', time.time() + 300 * attempts,
                         '精读尚未完成或输出未通过校验；已保留任务，可重试。', time.time(), identifier))

    def publish_ready(self):
        with self.db() as db:
            rows = db.execute("SELECT paper_id,result FROM tasks WHERE state='ready'").fetchall()
        if not rows:
            return
        if self.fetch is None:
            from tools.recommendation_data import fetch
        else:
            fetch = self.fetch
        confirmed, waiting = [], []
        for row in rows:
            try:
                actual = public_analysis(fetch('auto-reading/' + row['paper_id'] + '.json'))
                if actual == json.loads(row['result']):
                    confirmed.append(row)
                    continue
            except Exception:
                pass
            waiting.append(row)
        from tools.publish_reading import publish
        if waiting:
            (self.publisher or publish)(self.root, [json.loads(r['result']) for r in waiting])
        with self.db() as db:
            # Only acknowledge the exact submitted result, never a newer edit.
            for row in confirmed:
                db.execute("UPDATE tasks SET state='published',error='' WHERE paper_id=? AND result=? AND state='ready'",
                           (row['paper_id'], row['result']))
            for row in waiting:
                db.execute("UPDATE tasks SET error='精读已完成，正在等待发布结果核对。' WHERE paper_id=? AND state='ready'", (row['paper_id'],))

    async def run(self):
        while True:
            await asyncio.sleep(10)
            if time.time() < self.quiet_until or self.lock.locked():
                continue
            if time.time() - self.last_sync >= 600:
                self.last_sync = time.time()
                try:
                    await asyncio.to_thread(self.sync)
                except Exception:
                    self.sync_state = 'retry'
            if time.time() - self.last_publish >= 300:
                self.last_publish = time.time()
                try:
                    await asyncio.to_thread(self.publish_ready)
                except Exception:
                    with self.db() as db:
                        db.execute("UPDATE tasks SET error='精读已完成，发布未完成；结果已保留，将自动重试。' WHERE state='ready'")
            if time.time() < self.quiet_until or self.lock.locked():
                continue
            with self.db() as db:
                row = db.execute("SELECT * FROM tasks WHERE state IN ('pending','retry') AND attempts<3 AND next_attempt<=? ORDER BY priority DESC,updated_at LIMIT 1", (time.time(),)).fetchone()
            if row:
                self.active = asyncio.create_task(self.generate(dict(row)))
                try:
                    await self.active
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                finally:
                    self.active = None

    def start(self):
        self.runner = asyncio.create_task(self.run())

    async def close(self):
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
