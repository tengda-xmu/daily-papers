import asyncio
from copy import deepcopy
import json
import pytest

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


def test_shutdown_cancels_runner_instead_of_leaving_an_orphan(tmp_path, monkeypatch):
    async def scenario():
        started = asyncio.Event()
        class Waiting(FakeClient):
            async def turn(self, thread, prompt):
                started.set()
                yield {'type': 'delta', 'text': 'partial'}
                await asyncio.Event().wait()
        p = sample()
        q = ReadingQueue(tmp_path, tmp_path/'runtime', Waiting({}), asyncio.Lock(),
            resolver=lambda *a, **kw: {'basis':'abstract','version':'a'*64,'text':p['abstract'],'source_url':p['landing_url']})
        q.enqueue(p); q.quiet_until = 0; q.sync = lambda: None; q.publish_ready = lambda: None
        sleep = asyncio.sleep
        async def tick(_): await sleep(0)
        monkeypatch.setattr('connectors.codex_bridge.reading_queue.asyncio.sleep', tick)
        q.start()
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(q.close(), 1)
        assert q.runner.done() and not q.lock.locked()
        assert task(q)['state'] == 'pending' and task(q)['attempts'] == 0
    asyncio.run(scenario())


def test_new_fulltext_link_wakes_existing_task_without_changing_identity(tmp_path):
    p = sample(); q = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient({}), asyncio.Lock())
    q.enqueue(p)
    with q.db() as db:
        db.execute("UPDATE tasks SET state='missing_evidence',next_material=9999999999,result='kept'")
    q.enqueue({**p,'pdf_url':'https://www.nature.com/articles/example.pdf'})
    row = task(q)
    assert row['state'] == 'pending' and row['next_material'] == 0
    assert row['result'] == 'kept' and row['fingerprint'] == fingerprint(p)
    assert json.loads(row['paper'])['pdf_url'].endswith('example.pdf')


def test_new_local_pdf_wakes_published_task_but_never_enqueues_private_papers(tmp_path):
    p=sample(); q=ReadingQueue(tmp_path,tmp_path/'runtime',FakeClient({}),asyncio.Lock())
    q.enqueue(p)
    with q.db() as db:
        db.execute("UPDATE tasks SET state='missing_evidence',next_material=9999999999")
    q.resolver.signature=lambda identifier:'new-pdf-hash'
    q.document_available(p['id'])
    assert task(q)['state']=='pending' and task(q)['next_material']==0
    q.document_available('000000000000')
    assert len(q.snapshot()['tasks'])==1


@pytest.mark.parametrize('stage', ['start', 'thread'])
def test_connection_retries_do_not_exhaust_generation_attempts(tmp_path, monkeypatch, stage):
    from connectors.codex_bridge.rpc import CodexError
    from src.auto_reading import public_status, status_label
    now = 1790934000
    monkeypatch.setattr('connectors.codex_bridge.reading_queue.time.time', lambda: now)
    class Unavailable(FakeClient):
        async def start(self):
            if stage == 'start':
                raise CodexError('Private diagnostic must not become public', code='codex_version')
        async def thread(self, existing=None):
            raise CodexError('Private diagnostic must not become public')
    p = sample(); q = ReadingQueue(tmp_path, tmp_path/'runtime', Unavailable({}), asyncio.Lock())
    q.enqueue(p)
    for count, delay in enumerate((300, 900, 1800, 3600, 3600), 1):
        asyncio.run(q.generate(task(q)))
        current = task(q)
        assert current['state'] == 'retry' and current['attempts'] == 0
        assert current['connection_failures'] == count and current['next_attempt'] == now + delay
        assert 'Private' not in current['error']
    snapshot = q.snapshot()['tasks'][0]
    value = public_status({**snapshot, 'updated_at': '2026-10-02T09:40:00+00:00'})
    assert value['error_code'] == ('codex_version' if stage == 'start' else 'codex_connection')
    assert '10-02 18:40' in status_label(value)
    q2 = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient(response(p)), asyncio.Lock())
    assert task(q2)['connection_failures'] == 5 and task(q2)['next_attempt'] == now + 3600
    asyncio.run(q2.generate(task(q2)))
    assert task(q2)['state'] == 'ready' and task(q2)['attempts'] == 1
    assert task(q2)['connection_failures'] == 0 and task(q2)['error_code'] == ''


def test_only_proven_pre_generation_failures_are_migrated_once(tmp_path):
    q = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient({}), asyncio.Lock())
    p = sample()
    for index in range(3):
        q.enqueue({**p, 'id': str(index)*12})
    with q.db() as db:
        db.execute("DELETE FROM reading_migrations WHERE name='connection-attempts-v1'")
        db.execute("UPDATE tasks SET state='failed',attempts=3,error='精读尚未完成或输出未通过校验；已保留任务。CodexError'")
        db.execute("UPDATE tasks SET thread_id='started',draft='kept' WHERE paper_id=?", ('1'*12,))
        db.execute("UPDATE tasks SET error='Invalid evidence',checkpoint='kept' WHERE paper_id=?", ('2'*12,))
    q = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient({}), asyncio.Lock())
    with q.db() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY paper_id')]
        assert rows[0]['state'] == 'retry' and rows[0]['attempts'] == 0 and rows[0]['next_attempt'] == 0
        assert rows[1]['state'] == rows[2]['state'] == 'failed'
        assert rows[1]['draft'] == rows[2]['checkpoint'] == 'kept'
        db.execute("UPDATE tasks SET next_attempt=9999999999 WHERE paper_id=?", ('0'*12,))
    q = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient({}), asyncio.Lock())
    with q.db() as db:
        assert db.execute('SELECT next_attempt FROM tasks WHERE paper_id=?', ('0'*12,)).fetchone()[0] == 9999999999


def test_connection_failure_cannot_overwrite_new_document_revision(tmp_path):
    p = sample()
    q = ReadingQueue(tmp_path, tmp_path/'runtime', FakeClient({}), asyncio.Lock())
    q.enqueue(p); old = task(q)
    q.enqueue({**p, 'pdf_url': 'https://www.nature.com/articles/replacement.pdf'})
    q.connection_failed(old, TimeoutError())
    assert task(q)['revision'] == old['revision'] + 1
    assert task(q)['state'] == 'pending' and task(q)['connection_failures'] == 0


def test_terminal_failure_does_not_promise_automatic_generation_retry():
    from src.auto_reading import status_label
    label = status_label({'state': 'failed', 'basis': 'abstract'})
    assert '自动尝试已用尽' in label and '等待自动重试' not in label


def test_runner_automatically_retries_connection_when_due(tmp_path, monkeypatch):
    async def scenario():
        p = sample(); clock = [1790934000]; calls=[]; published=asyncio.Event()
        sleep = asyncio.sleep
        async def tick(seconds):
            clock[0] += seconds
            await sleep(0)
        class Recovered(FakeClient):
            async def start(self):
                calls.append(clock[0])
                if len(calls) == 1:
                    raise TimeoutError('offline')
        q = ReadingQueue(tmp_path, tmp_path/'runtime', Recovered(response(p)), asyncio.Lock(),
            resolver=lambda *a, **kw: {'basis':'abstract','version':'a'*64,'text':p['abstract'],'source_url':p['landing_url']})
        q.enqueue(p); q.quiet_until=0; q.sync=lambda: None
        q.publish_ready=lambda: published.set() if task(q)['state']=='ready' else None
        monkeypatch.setattr('connectors.codex_bridge.reading_queue.time.time',lambda:clock[0])
        monkeypatch.setattr('connectors.codex_bridge.reading_queue.asyncio.sleep',tick)
        q.start()
        try:
            await asyncio.wait_for(published.wait(),2)
            assert len(calls)==2 and calls[1]-calls[0]>=300
            assert task(q)['state']=='ready' and task(q)['attempts']==1
        finally:
            await q.close()
    asyncio.run(scenario())
