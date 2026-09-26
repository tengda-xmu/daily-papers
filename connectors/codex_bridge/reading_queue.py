"""Resumable, low-priority local Codex enrichment; isolated from personal chats."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import hashlib
import re
from pathlib import Path
import sqlite3
import time

from src.auto_reading import READER_VERSION, fingerprint, prompt, public_analysis, public_paper, public_status, status_label
from src.editions import read, relative_path, selected, write
from src.reading_notes import valid_analysis


class SupersededReading(Exception):
    pass


class ReadingQueue:
    paper_queue = True

    def __init__(self, root, runtime, client, generation_lock, *, fetch=None, publisher=None, resolver=None, documents=None, acquired=None):
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
            columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
            if 'enabled' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
            if 'material' not in columns:
                db.commit()
                backup = self.runtime / 'before-fulltext-queue.sqlite3'
                if self.paper_queue and not backup.exists():
                    with sqlite3.connect(backup) as target:
                        db.backup(target)
                for name, declaration in (
                    ('material', "TEXT NOT NULL DEFAULT ''"), ('material_version', "TEXT NOT NULL DEFAULT ''"),
                    ('next_material', 'REAL NOT NULL DEFAULT 0'), ('published_result', "TEXT NOT NULL DEFAULT ''"),
                    ('checkpoint', "TEXT NOT NULL DEFAULT ''"), ('local_signature', "TEXT NOT NULL DEFAULT ''")):
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')
                if self.paper_queue:
                    db.execute("UPDATE tasks SET published_result=CASE WHEN state='published' THEN COALESCE(result,'') ELSE '' END, state='pending',attempts=0,next_attempt=0,thread_id=NULL")
            db.execute("UPDATE tasks SET state='pending', attempts=MAX(0,attempts-1) WHERE state='generating'")
            db.execute("UPDATE tasks SET state='pending' WHERE state='fetching'")
            columns = {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
            for name, declaration in (('revision', 'INTEGER NOT NULL DEFAULT 0'), ('result_revision', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('reader_version', 'INTEGER NOT NULL DEFAULT 0'), ('wanted_signature', "TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    db.execute(f'ALTER TABLE tasks ADD COLUMN {name} {declaration}')
            if self.paper_queue:
                for row in db.execute('SELECT paper_id,state,result FROM tasks WHERE reader_version!=?', (READER_VERSION,)).fetchall():
                    value = json.loads(row['result'] or '{}')
                    if not (row['state'] in ('published', 'ready') and value.get('analysis', {}).get('analysis_basis') == 'full_text'):
                        db.execute("UPDATE tasks SET state='pending',attempts=0,next_attempt=0,next_material=0,checkpoint='',thread_id=NULL,error='' WHERE paper_id=?", (row['paper_id'],))
                db.execute('UPDATE tasks SET reader_version=?,wanted_signature=local_signature WHERE reader_version!=?', (READER_VERSION, READER_VERSION))
        self.fetch, self.publisher = fetch, publisher
        self.runner = self.active = None
        self.quiet_until = time.time() + 30
        self.last_sync = self.last_publish = 0
        self.sync_state = 'pending'
        self.acquired = acquired
        if self.paper_queue:
            from .reading_materials import MaterialResolver
            self.resolver = resolver or MaterialResolver(self.runtime, documents=documents)

    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def assert_current(self, row):
        with self.db() as db:
            current = db.execute('SELECT revision,enabled FROM tasks WHERE paper_id=?', (row['paper_id'],)).fetchone()
        if not current or not current['enabled'] or current['revision'] != row['revision']:
            raise SupersededReading()

    def enqueue(self, paper, priority=0):
        if not re.fullmatch(r'[a-f0-9]{12}', str(paper.get('id', ''))):
            return
        clean = public_paper(paper)
        clean.update({k: paper[k] for k in ('source', 'oa_url', 'pdf_url') if paper.get(k)})
        key = fingerprint(clean)
        state = 'pending'
        signature = self.resolver.signature(paper['id']) if hasattr(self.resolver, 'signature') else ''
        with self.db() as db:
            old = db.execute('SELECT * FROM tasks WHERE paper_id=?', (paper['id'],)).fetchone()
            if old:
                db.execute('UPDATE tasks SET priority=?,enabled=1 WHERE paper_id=?', (priority, paper['id']))
                previous = json.loads(old['paper'])
                hints_changed = any(previous.get(k) != clean.get(k) for k in ('source', 'oa_url', 'pdf_url'))
                if old['fingerprint'] != key or hints_changed or signature != old['wanted_signature']:
                    db.execute("""UPDATE tasks SET paper=?,fingerprint=?,state='pending',attempts=0,next_material=0,next_attempt=0,
                        wanted_signature=?,revision=revision+1,error='',updated_at=? WHERE paper_id=?""",
                        (json.dumps(clean, ensure_ascii=False), key, signature, time.time(), paper['id']))
                return
            db.execute('''INSERT INTO tasks(paper_id,paper,fingerprint,state,updated_at,priority,wanted_signature,reader_version)
                          VALUES(?,?,?,?,?,?,?,?)''',
                (paper['id'], json.dumps(clean, ensure_ascii=False), key, state, time.time(), priority, signature, READER_VERSION))

    def sync(self):
        if self.fetch is None:
            from tools.recommendation_data import fetch
        else:
            fetch = self.fetch
        manifest = fetch('editions/index.json')
        deleted = {e['id']: e for e in read(self.runtime / 'edition-manifest.json').get('deleted', [])}
        deleted.update({e['id']: e for e in manifest.get('deleted', [])})
        manifest = {**manifest, 'deleted': list(deleted.values()),
                    'editions': [e for e in manifest.get('editions', []) if e['id'] not in deleted]}
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
        write(self.runtime / 'edition-manifest.json', manifest)
        with self.db() as db:
            db.execute('UPDATE tasks SET enabled=0')
        for paper in papers.values():
            priority = 200 if paper['id'] in {p['id'] for p in latest.get('core', [])} else 100 if paper['id'] in {p['id'] for p in latest.get('extended', [])} else 0
            self.enqueue(paper, priority=priority)
        self.sync_state = 'ok'

    def document_available(self, identifier):
        # Only published recommendations belong to this public enrichment queue.
        # Personal/manual-search documents must never create publication tasks.
        with self.db() as db:
            row = db.execute('SELECT paper,priority FROM tasks WHERE paper_id=? AND enabled=1', (identifier,)).fetchone()
        if row:
            self.enqueue(json.loads(row['paper']), priority=row['priority'])
        self.last_sync = 0
        return next((r for r in self.snapshot()['tasks'] if r['paper_id'] == identifier and r['enabled']), None)

    def snapshot(self):
        with self.db() as db:
            rows = db.execute('SELECT * FROM tasks ORDER BY priority DESC,updated_at DESC').fetchall()
        items = [{**{k: row[k] for k in ('paper_id', 'state', 'attempts', 'updated_at', 'error')},
                  'title': json.loads(row['paper']).get('title', ''),
                  'basis': (json.loads(row['material'] or '{}')).get('basis', ''),
                  'reason': (json.loads(row['material'] or '{}')).get('reason_code', ''), 'next_material': row['next_material'], 'enabled': bool(row['enabled'])} for row in rows]
        for row, item in zip(rows, items):
            material = json.loads(row['material'] or '{}')
            saved = json.loads(row['checkpoint'] or '{}')
            pages = saved.get('pages', {}) if saved.get('version') == material.get('version') else {}
            if row['wanted_signature'] != row['local_signature']:
                pages, material = {}, {}
            result = json.loads(row['result'] or '{}').get('material', {})
            item['completed_at'] = json.loads(row['result'] or '{}').get('analysis', {}).get('analyzed_at', '')
            item.update(pdf_available=bool(row['wanted_signature'] or material.get('document', {}).get('kind') == 'pdf'),
                        total=len(material.get('document', {}).get('pages', [])),
                        covered=sum(p['status'] in ('read', 'source_defect') for p in pages.values()),
                        issues=[i for p in pages.values() for i in p.get('issues', [])])
            if item['state'] in ('published', 'ready'):
                item.update(total=result.get('total', item['total']), covered=result.get('covered', item['covered']), issues=result.get('issues', item['issues']))
            item['label'] = status_label(item)
        return {'sync_state': self.sync_state, 'tasks': items,
                'counts': {state: sum(r['state'] == state and r['enabled'] for r in items) for state in
                           ('pending', 'fetching', 'generating', 'ready', 'published', 'retry', 'failed', 'missing_evidence', 'awaiting_fulltext')}}

    def retry(self, identifier):
        with self.db() as db:
            row = db.execute('SELECT state FROM tasks WHERE paper_id=? AND enabled=1', (identifier,)).fetchone()
            if not row or row['state'] not in ('failed', 'retry', 'missing_evidence', 'awaiting_fulltext'):
                raise ValueError('该论文当前没有可重试的精读任务。')
            db.execute("UPDATE tasks SET state='pending',attempts=0,next_attempt=0,next_material=0,error='' WHERE paper_id=?", (identifier,))
        return self.snapshot()

    async def preempt(self):
        self.quiet_until = time.time() + 120
        if self.active and not self.active.done():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)

    async def process(self, row):
        identifier = row['paper_id']
        try:
            self.assert_current(row)
            with self.db() as db:
                db.execute("UPDATE tasks SET state='fetching',updated_at=? WHERE paper_id=?", (time.time(), identifier))
            material = await asyncio.to_thread(self.resolver, json.loads(row['paper']), force=row['next_material'] == 0)
            self.assert_current(row)
            if material.get('document', {}).get('kind') == 'pdf' and self.acquired:
                await asyncio.to_thread(self.acquired, identifier, material)
                self.assert_current(row)
                material['local_signature'] = self.resolver.signature(identifier)
            encoded = json.dumps(material, ensure_ascii=False)
            with self.db() as db:
                db.execute('UPDATE tasks SET material=?,next_material=?,local_signature=?,wanted_signature=? WHERE paper_id=? AND revision=?',
                           (encoded, time.time() + 86400, material.get('local_signature', ''), material.get('local_signature', row['wanted_signature']), identifier, row['revision']))
                if db.execute('SELECT revision FROM tasks WHERE paper_id=?', (identifier,)).fetchone()[0] != row['revision']:
                    raise SupersededReading()
                if material['basis'] == 'missing':
                    db.execute("UPDATE tasks SET state='missing_evidence',error=?,updated_at=? WHERE paper_id=?",
                               (material.get('reason', '暂时无法获取资料，将自动重试。'), time.time(), identifier))
                    return
                old = json.loads(row['result'] or '{}')
                if old.get('fingerprint') == row['fingerprint'] and old.get('material', {}).get('version') == material['version']:
                    state = 'ready' if row['result'] != row['published_result'] else 'published' if material['basis'] == 'full_text' else 'awaiting_fulltext'
                    db.execute('UPDATE tasks SET state=?,result_revision=revision,error=? WHERE paper_id=?', (state, material.get('reason', ''), identifier))
                    return
                if material['version'] != row['material_version']:
                    db.execute("UPDATE tasks SET attempts=0,checkpoint='',thread_id=NULL,material_version=? WHERE paper_id=?", (material['version'], identifier))
                elif row['attempts'] >= 3:
                    db.execute("UPDATE tasks SET state='failed' WHERE paper_id=?", (identifier,))
                    return
                db.execute("UPDATE tasks SET state='pending',error='',updated_at=? WHERE paper_id=?", (time.time(), identifier))
                fresh = dict(db.execute('SELECT * FROM tasks WHERE paper_id=?', (identifier,)).fetchone())
            if time.time() >= self.quiet_until and not self.lock.locked():
                await self.generate(fresh)
        except SupersededReading:
            return
        except asyncio.CancelledError:
            with self.db() as db:
                db.execute("UPDATE tasks SET state='pending' WHERE paper_id=? AND revision=? AND state='fetching'", (identifier, row['revision']))
            raise
        except Exception:
            with self.db() as db:
                db.execute("UPDATE tasks SET state='missing_evidence',next_material=?,error=?,updated_at=? WHERE paper_id=? AND revision=?",
                           (time.time() + 86400, '资料获取暂未完成，将自动重试。', time.time(), identifier, row['revision']))

    async def generate(self, row):
        identifier, paper = row['paper_id'], json.loads(row['paper'])
        material = json.loads(row.get('material') or '{}') or {
            'basis': 'abstract', 'text': paper.get('abstract', ''), 'source_url': paper.get('landing_url', ''),
            'version': hashlib.sha256(paper.get('abstract', '').encode()).hexdigest()}
        async with self.lock:
            self.assert_current(row)
            with self.db() as db:
                db.execute("UPDATE tasks SET state='generating',attempts=attempts+1,error='',updated_at=? WHERE paper_id=?", (time.time(), identifier))
            text = ''
            try:
                await self.client.start()
                thread = await self.client.thread(row['thread_id'])
                with self.db() as db:
                    db.execute('UPDATE tasks SET thread_id=? WHERE paper_id=?', (thread, identifier))
                from .reading_document import read_document, coverage
                notes, ledger = await read_document(self, row, material, thread) if material['basis'] == 'full_text' else ('', {})
                text, saved = '', 0
                async for event in self.client.turn(thread, prompt(paper, material, notes)):
                    self.assert_current(row)
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
                analysis.update(analysis_status='ready', analysis_basis=material['basis'], analysis_kind='model',
                    analysis_sources=[material['source_url']], analyzed_at=datetime.now(timezone.utc).isoformat(),
                    llm_model=self.client.model)
                # Verified page defects accompany every final reading, even if
                # the synthesis model omits them from its prose.
                defects = [i for p in ledger.get('pages', {}).values() for i in p.get('issues', []) if i['kind'] == 'source_defect']
                if defects and isinstance(analysis.get('deep_read', {}).get('limitations'), str):
                    analysis['deep_read']['limitations'] += '\n原文核对：' + '；'.join(f"[{i['page']}] {i['detail']}" for i in defects)
                evidence = (raw.get('evaluation') or {}).get('evidence', '')
                if evidence and ' '.join(evidence.casefold().split()) not in ' '.join(material['text'].casefold().split()):
                    if (material.get('document') or {}).get('scan_pages'):
                        raw['evaluation']['evidence'] = evidence = ''
                        raw['evaluation']['evidence_sufficient'] = False
                    else:
                        raise ValueError('Assessment evidence is not in the provided material')
                pages = (material.get('document') or {}).get('pages', [])
                refs = raw.get('references', {}) if pages else {}
                labels = {p['label'] for p in pages}
                if not isinstance(refs, dict) or any(not isinstance(v, list) or any(p not in labels for p in v) for v in refs.values()):
                    raise ValueError('Unknown page reference')
                value = public_analysis({'paper_id': identifier, 'fingerprint': row['fingerprint'],
                    'analysis': analysis, 'evaluation': raw.get('evaluation'),
                    'material': {'version': material['version'], 'total': len(pages) or 1,
                                 'covered': len(coverage(ledger)) if pages else 1, 'references': refs, 'excerpt': evidence,
                                 **({'reader_version': READER_VERSION, 'covered_labels': coverage(ledger),
                                     'issues': [i for p in ledger['pages'].values() for i in p.get('issues', [])]} if pages else {})}})
                self.assert_current(row)
                write(self.runtime / 'reading-results' / (identifier + '.json'), value)
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='ready',result=?,result_revision=revision,draft='',next_material=?,updated_at=? WHERE paper_id=? AND revision=?",
                               (json.dumps(value, ensure_ascii=False), time.time() + 86400, time.time(), identifier, row['revision']))
                self.last_publish = 0
            except SupersededReading:
                return
            except asyncio.CancelledError:
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1) WHERE paper_id=? AND revision=?", (identifier, row['revision']))
                raise
            except Exception as exc:
                with self.db() as db:
                    attempts = db.execute('SELECT attempts FROM tasks WHERE paper_id=?', (identifier,)).fetchone()[0]
                    reason = str(exc)[:180] if isinstance(exc, ValueError) else type(exc).__name__
                    db.execute('UPDATE tasks SET state=?,next_attempt=?,error=?,draft=?,updated_at=? WHERE paper_id=? AND revision=?',
                        ('failed' if attempts >= 3 else 'retry', time.time() + 300 * attempts,
                         '精读尚未完成或输出未通过校验；已保留任务。' + reason, text[:50000], time.time(), identifier, row['revision']))

    def publish_ready(self):
        if self.paper_queue:
            self.apply_deletions()
        with self.db() as db:
            rows = db.execute("SELECT paper_id,result,fingerprint,revision FROM tasks WHERE enabled=1 AND result_revision=revision AND result IS NOT NULL AND result!=published_result").fetchall()
        rows = [row for row in rows if json.loads(row['result']).get('fingerprint') == row['fingerprint']]
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
        statuses = []
        if not self.publisher:
            for row in self.snapshot()['tasks']:
                if not row['enabled']:
                    continue
                if row['state'] in ('pending', 'fetching', 'generating'):
                    continue  # Publish stable outcomes, not a rebuild for each transient step.
                # This status travels in the same deployment as the completed
                # reading. Keep the local task ready until its online copy is
                # verified, but never publish a stale "waiting to publish" label.
                public_state = row['state']
                if public_state == 'ready':
                    public_state = 'published' if row['basis'] == 'full_text' else 'awaiting_fulltext'
                value = public_status({'paper_id': row['paper_id'], 'state': public_state, 'basis': row['basis'],
                    'reason': row['reason'], **{k: row[k] for k in ('total', 'covered', 'pdf_available', 'issues')},
                    'updated_at': datetime.fromtimestamp(row['updated_at'], timezone.utc).isoformat()})
                path = self.runtime / 'published-reading-status' / (row['paper_id'] + '.json')
                if read(path) != value:
                    try:
                        actual = public_status(fetch('reading-status/' + row['paper_id'] + '.json'))
                    except Exception:
                        actual = None
                    if actual == value:
                        write(path, value)
                    else:
                        statuses.append(value)
        if waiting or statuses:
            for row in waiting:
                self.assert_current(row)
            if self.publisher:
                self.publisher(self.root, [json.loads(r['result']) for r in waiting])
            else:
                publish(self.root, [json.loads(r['result']) for r in waiting], statuses=statuses)
        with self.db() as db:
            # Only acknowledge the exact submitted result, never a newer edit.
            for row in confirmed:
                full = json.loads(row['result'])['analysis']['analysis_basis'] == 'full_text'
                db.execute("UPDATE tasks SET published_result=?,state=CASE WHEN state='ready' THEN ? ELSE state END,error='' WHERE paper_id=? AND result=?",
                           (row['result'], 'published' if full else 'awaiting_fulltext', row['paper_id'], row['result']))
            for row in waiting:
                db.execute("UPDATE tasks SET error='精读已完成，正在等待发布结果核对。' WHERE paper_id=? AND state='ready'", (row['paper_id'],))

    def apply_deletions(self):
        """Stop only tasks whose public batch membership has been revoked."""
        from src.editions import manifest as local_manifest
        values = [local_manifest(self.root / 'data'), read(self.runtime / 'edition-manifest.json')]
        removed = {e['id'] for value in values for e in value.get('deleted', [])}
        if not removed:
            return
        retained, revoked = set(), set()
        for path in [* (self.runtime / 'public-editions').glob('*/*.json'),
                     * (self.runtime / 'recommendation-history').glob('*.json'),
                     * (self.root / 'data/editions').glob('*/*.json'), self.runtime / 'recommendations.json', self.root / 'data/daily.json']:
            payload = read(path)
            target = revoked if payload.get('edition', {}).get('id') in removed else retained
            target.update(p['id'] for p in selected(payload))
        with self.db() as db:
            db.executemany('UPDATE tasks SET enabled=0 WHERE paper_id=?', [(i,) for i in revoked - retained])

    async def run(self):
        while True:
            await asyncio.sleep(10)
            if time.time() < self.quiet_until or self.lock.locked():
                continue
            if self.paper_queue:
                self.apply_deletions()
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
                if self.paper_queue:
                    row = db.execute("""SELECT * FROM tasks WHERE enabled=1 AND (
                        (state IN ('pending','retry') AND attempts<3 AND next_attempt<=?) OR
                        (state IN ('missing_evidence','awaiting_fulltext','failed') AND next_material<=?))
                        ORDER BY priority DESC,updated_at LIMIT 1""", (time.time(), time.time())).fetchone()
                else:
                    row = db.execute("SELECT * FROM tasks WHERE state IN ('pending','retry') AND attempts<3 AND next_attempt<=? ORDER BY priority DESC,updated_at LIMIT 1", (time.time(),)).fetchone()
            if row:
                work = self.process if self.paper_queue else self.generate
                self.active = asyncio.create_task(work(dict(row)))
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
