import asyncio
from copy import deepcopy
import json

from connectors.codex_bridge.reading_queue import ReadingQueue
from src.auto_reading import fingerprint, public_analysis
from src.reading_notes import curated_entries
from src.editions import append, entries, relative_path


def sample():
    p = deepcopy(curated_entries()[0]['paper'])
    p.update(id='123456abcdef', topic_tags=['ai_maintenance'])
    return p


def response(p):
    return {'analysis': deepcopy(curated_entries()[0]['analysis']), 'evaluation': {
        'direction_fit': True, 'research_value': True, 'evidence_sufficient': True,
        'reason': '论文提供了与工程监测和结构可靠性相关的明确方法及验证结果，可作为后续研究的重要参考。',
        'evidence': p['abstract'][:80]}}


class FakeClient:
    model = 'fixture-model'
    def __init__(self, value):
        self.value = value
    async def start(self):
        pass
    async def thread(self, existing=None):
        return existing or 'task-thread'
    async def turn(self, thread, prompt):
        if isinstance(self.value, Exception):
            raise self.value
        yield {'type': 'delta', 'text': json.dumps(self.value, ensure_ascii=False)}
        yield {'type': 'completed', 'status': 'completed'}


def task(queue):
    with queue.db() as db:
        return dict(db.execute('SELECT * FROM tasks').fetchone())


def test_validated_notes_and_publish_acknowledgement(tmp_path):
    p = sample(); q = ReadingQueue(tmp_path, tmp_path / 'runtime', FakeClient(response(p)), asyncio.Lock())
    q.enqueue(p)
    asyncio.run(q.generate(task(q)))
    ready = task(q)
    assert ready['state'] == 'ready' and ready['attempts'] == 1
    value = json.loads(ready['result'])
    assert value['fingerprint'] == fingerprint(p) and value['analysis']['analysis_basis'] == 'abstract'
    calls = []; q.publisher = lambda root, values: calls.append(values)
    q.fetch = lambda path: {}
    q.publish_ready()
    assert len(calls) == 1 and task(q)['state'] == 'ready'  # Dispatch is not publication.
    q.fetch = lambda path: value
    q.publish_ready()
    assert task(q)['state'] == 'awaiting_fulltext' and len(calls) == 1


def test_three_failures_survive_restart_and_manual_retry(tmp_path):
    p = sample(); client = FakeClient(TimeoutError())
    q = ReadingQueue(tmp_path, tmp_path / 'runtime', client, asyncio.Lock())
    q.enqueue(p)
    for _ in range(3):
        asyncio.run(q.generate(task(q)))
    assert task(q)['state'] == 'failed' and task(q)['attempts'] == 3
    q2 = ReadingQueue(tmp_path, tmp_path / 'runtime', client, asyncio.Lock())
    q2.enqueue(p)
    assert task(q2)['state'] == 'failed'
    q2.retry(p['id'])
    assert task(q2)['state'] == 'pending' and task(q2)['attempts'] == 0


def test_missing_evidence_no_false_completion(tmp_path):
    p = sample(); p['abstract'] = ''
    q = ReadingQueue(tmp_path, tmp_path / 'runtime', FakeClient({}), asyncio.Lock())
    q.enqueue(p)
    assert task(q)['state'] == 'pending'
    q.resolver = lambda paper, **kwargs: {'basis': 'missing', 'reason': 'Unavailable'}
    asyncio.run(q.process(task(q)))
    assert task(q)['state'] == 'missing_evidence' and task(q)['attempts'] == 0
    assert task(q)['next_material'] > 0


def test_user_preempts_background_without_consuming_retry(tmp_path):
    async def scenario():
        started = asyncio.Event()
        class Waiting(FakeClient):
            async def turn(self, thread, prompt):
                started.set()
                yield {'type': 'delta', 'text': 'partial progress'}
                await asyncio.Event().wait()
        p = sample(); lock = asyncio.Lock()
        q = ReadingQueue(tmp_path, tmp_path / 'runtime', Waiting({}), lock)
        q.enqueue(p); q.active = asyncio.create_task(q.generate(task(q)))
        await started.wait(); assert lock.locked()
        await q.preempt()
        assert not lock.locked() and task(q)['state'] == 'pending' and task(q)['attempts'] == 0
    asyncio.run(scenario())


def test_invalid_evidence_not_published(tmp_path):
    p = sample(); raw = response(p); raw['evaluation']['evidence'] = 'This claim does not occur in the actual provided abstract.'
    q = ReadingQueue(tmp_path, tmp_path / 'runtime', FakeClient(raw), asyncio.Lock())
    q.enqueue(p); asyncio.run(q.generate(task(q)))
    assert task(q)['state'] == 'retry' and task(q)['result'] is None


def test_sync_only_published_papers_and_same_snapshot_does_not_reset(tmp_path):
    p = sample()
    payload = append(tmp_path / 'data', {'generated_at': '2026-09-25T00:00:00+00:00', 'update_run_id':'1', 'core':[p], 'extended':[]})
    public = {'editions/index.json': {'editions': entries(tmp_path / 'data')},
              relative_path(payload['edition']): payload, 'data.json': payload}
    q = ReadingQueue(tmp_path, tmp_path / 'runtime', FakeClient(response(p)), asyncio.Lock(), fetch=public.__getitem__)
    q.sync(); asyncio.run(q.generate(task(q))); q.sync()
    assert len(q.snapshot()['tasks']) == 1 and task(q)['state'] == 'ready'


def test_public_analysis_strips_unrelated_fields():
    p = sample(); raw = response(p)
    raw.update(paper_id=p['id'], fingerprint=fingerprint(p), credentials='secret')
    raw['analysis']['analysis_basis'] = 'abstract'
    raw['analysis']['deep_read']['private'] = 'secret'
    raw['evaluation']['private'] = 'secret'
    clean = public_analysis(raw)
    assert 'secret' not in json.dumps(clean)


def test_latest_edition_priority_preserves_draft_and_attempts(tmp_path):
    p = sample(); q = ReadingQueue(tmp_path, tmp_path / 'runtime', FakeClient({}), asyncio.Lock())
    q.enqueue(p)
    with q.db() as db:
        db.execute("UPDATE tasks SET state='retry',draft='kept',attempts=1")
    q.enqueue(p, priority=100)
    assert task(q)['priority'] == 100 and task(q)['draft'] == 'kept' and task(q)['attempts'] == 1
