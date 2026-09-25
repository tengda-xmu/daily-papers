"""AI column notes share the low-priority runner, with an independent task DB."""
import asyncio
from datetime import datetime, timezone
import json
import time

from .reading_queue import ReadingQueue
from src.ai_updates import fingerprint, normalize, prompt, validate_analysis, can_analyze
from src.public_sources import write


class AIQueue(ReadingQueue):
    paper_queue = False
    def __init__(self, root, runtime, client, generation_lock, **kwargs):
        super().__init__(root, runtime / 'ai-digests', client, generation_lock, **kwargs)

    def enqueue(self, item, priority=0):
        item = normalize(item)
        key = fingerprint(item)
        state = 'pending' if can_analyze(item) else 'missing_evidence'
        if item.get('analysis_status') == 'ready':
            state = 'published'
        with self.db() as db:
            old = db.execute('SELECT * FROM tasks WHERE paper_id=?', (item['id'],)).fetchone()
            if old and old['fingerprint'] == key:
                if state == 'published':
                    db.execute("UPDATE tasks SET state='published',error='' WHERE paper_id=? AND state!='generating'", (item['id'],))
                return
            if old and old['state'] == 'generating':
                return
            db.execute('''INSERT INTO tasks(paper_id,paper,fingerprint,state,updated_at,priority) VALUES(?,?,?,?,?,?)
                ON CONFLICT(paper_id) DO UPDATE SET paper=excluded.paper,fingerprint=excluded.fingerprint,
                state=excluded.state,attempts=0,next_attempt=0,result=NULL,error='',thread_id=NULL,draft='',updated_at=excluded.updated_at''',
                (item['id'], json.dumps(item, ensure_ascii=False), key, state, time.time(), priority))

    def sync(self):
        from tools.recommendation_data import fetch
        data = (self.fetch or fetch)('ai-updates.json')
        for item in sorted(data.get('entries', []) + data.get('social_readings', []), key=lambda r: r.get('published_at', ''), reverse=True):
            self.enqueue(item)
        self.sync_state = 'ok'

    async def generate(self, row):
        item = json.loads(row['paper'])
        async with self.lock:
            with self.db() as db:
                db.execute("UPDATE tasks SET state='generating',attempts=attempts+1,error='',updated_at=? WHERE paper_id=?", (time.time(), item['id']))
            try:
                await self.client.start()
                thread = await self.client.thread(row['thread_id'])
                with self.db() as db:
                    db.execute('UPDATE tasks SET thread_id=? WHERE paper_id=?', (thread, item['id']))
                text = ''
                async for event in self.client.turn(thread, prompt(item)):
                    if event['type'] == 'delta':
                        text += event.get('text', '')
                        if len(text) > 30000:
                            raise ValueError('AI output too long')
                    elif event['type'] == 'completed' and event.get('status') != 'completed':
                        raise ValueError('AI reading interrupted')
                text = text.strip()
                if text.startswith('```'):
                    text = text.split('\n', 1)[1].rsplit('```', 1)[0]
                value = validate_analysis(json.loads(text), item)
                write(self.runtime / 'reading-results' / (item['id'] + '.json'), value)
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='ready',result=?,draft='',updated_at=? WHERE paper_id=?",
                               (json.dumps(value, ensure_ascii=False), time.time(), item['id']))
            except asyncio.CancelledError:
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='pending',attempts=MAX(0,attempts-1) WHERE paper_id=?", (item['id'],))
                raise
            except Exception:
                with self.db() as db:
                    attempts = db.execute('SELECT attempts FROM tasks WHERE paper_id=?', (item['id'],)).fetchone()[0]
                    db.execute('UPDATE tasks SET state=?,next_attempt=?,error=?,updated_at=? WHERE paper_id=?',
                               ('failed' if attempts >= 3 else 'retry', time.time()+300*attempts,
                                'AI 导读未完成或未通过来源校验，可重试。', time.time(), item['id']))

    def publish_ready(self):
        from tools.publish_reading import publish_values
        from tools.recommendation_data import fetch
        with self.db() as db:
            rows = db.execute("SELECT paper_id,paper,result FROM tasks WHERE state='ready'").fetchall()
        if not rows:
            return
        data = (self.fetch or fetch)('ai-updates.json')
        current = {r['id']: r for r in data.get('entries', []) + data.get('social_readings', [])}
        values = []
        for row in rows:
            item = current.get(row['paper_id'])
            if not item or fingerprint(item) != json.loads(row['result'])['content_version']:
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='failed',error='官方内容已更新，请刷新任务后重试。' WHERE paper_id=?", (row['paper_id'],))
                continue
            value = validate_analysis(json.loads(row['result']), item)
            if item.get('analysis') == value['analysis']:
                with self.db() as db:
                    db.execute("UPDATE tasks SET state='published',error='' WHERE paper_id=? AND result=?", (row['paper_id'], row['result']))
            else:
                values.append(value)
        if values:
            if self.publisher:
                self.publisher(self.root, values)
            else:
                publish_values(self.root, values, 'ai-readings', 'id')
